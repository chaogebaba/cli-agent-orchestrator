# F541 (#397) + F542 (#398) — Build Report

## Provenance

| | |
|---|---|
| Base sha | `f6c35046` (`Merge 'cao/26643eb7' into main (F244 gated)`) |
| Branch | `cao/e08be272` (isolated worktree `e08be272`) |
| F541 commit | `a2f446a1` — `F541: launch confirm-then-attach on slow cold start (#397)` |
| F542 commit | `e2406a47` — `F542: reconcile dead-tmux-session terminal rows at startup (#398)` |
| F541 init-timeout commit | `97260c50` — `F541: provider_init_timeout default 180s for claude_code cold start (#397)` |
| F548 commit | `0ca9c2d0` — `F548: fix claude_code init dialog handling + fast auth-fail + pane tail (#404)` |
| F548 gate-test commit | `60f4cf37` — `F548: land gate SHOULD-1 regression test verbatim (#404)` |
| F548 r3-fix commit | `c5587aa4` — `F548: send arrow as real tmux Down key + settle poll; structured init errors (#404)` |
| F548 r6 commit | `7f07e277` — `F548 r6: post-idle 'Not logged in' auth scan -> fast E-CLAUDE-AUTH (#404)` |
| F548 r7 commit | `affe0438` — `F548 r7: gate S-1 settle-poll regression + S-2 bound post-idle scan + N-1 (#404)` |
| F548 r8 commit | `a14faba7` — `F548 r8: fix gate B-1 — extend settle-poll-affected test fixture (#404)` |
| Report commits | `b6eacf44` + `c5ebeebc`… (`r2`) + `b46bf972` (`r3`) + `61008d9d` (`r4`) + `5d4c410d` (`r5`) + `e2728529` (`r6`) + `26878496` (`r7`) + `F541/F542: build report r8` (this update) |

All paths below are relative to the worktree root
`/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/.cao/worktrees/e08be272/`.
Nothing has been pushed.

---

## F541 (#397) — `cao launch` reports read timeout on a slow-but-successful cold launch

Root cause per the issue: `cli/commands/launch.py` used
`get_server_settings()["mcp_request_timeout"]` (30s) as the HTTP read timeout for
`POST /sessions/start`, whose latency is bounded by provider init, not by an MCP
request. A timeout was then reported as a connection failure.

### Acceptance-criteria checklist

| # | AC (from #397) | How met | Evidence (file:line) |
|---|---|---|---|
| 1 | On read timeout, poll `GET /sessions/<session_name>` (bounded, ~120s); if the supervisor terminal exists, continue the normal path (attach, or headless exit 0 with the terminal id); never report a failure for a launch that succeeded. | `except requests.exceptions.Timeout` calls `_poll_session_supervisor_after_timeout(session_name)`, which polls `GET /sessions/<name>` until `terminals` is non-empty, then returns a `{id,name,session_name}` dict; the caller sets `terminal = confirmed` and runs the shared post-start path (`_finish_launch_after_start`) exactly as the normal POST path does. | poll helper `cli/commands/launch.py:114`; poll loop + `GET` `:142`,`:149`; supervisor-present return `cli/commands/launch.py` (`if terminals:` block inside the helper); timeout handler `:496`; `confirmed = ...` `:505`; `terminal = confirmed` `:518`; shared success path `:521` and `:560`; `_finish_launch_after_start` def `:183` |
| 2 | Launch-specific timeout (~120s) instead of `mcp_request_timeout`. | `SESSION_START_TIMEOUT_S = 120` is passed as the POST `timeout`; `mcp_request_timeout` is no longer used for `POST /sessions/start`. | constant `cli/commands/launch.py:63`; POST `post_kwargs: dict = {"params": params, "timeout": SESSION_START_TIMEOUT_S}` `:488` |
| 3 | Error text must distinguish "server unreachable" from "launch still initializing". | Two distinct `ClickException` messages: a `RequestException` raised **during the confirm poll** → `"cao-server became unreachable while confirming the launch"`; a poll that never finds the supervisor within the bound → `"Launch of session ... is still initializing after {N}s and could not be confirmed. The server is reachable but the supervisor terminal has not come up yet"`. The original outer `RequestException` handler (`"Failed to connect to cao-server"`) is preserved for a genuine connect failure on the initial POST. | unreachable branch `cli/commands/launch.py:507-509`; still-initializing branch `:510-517`; outer connect-failure handler unchanged (`Failed to connect to cao-server`) later in the same `except` chain |
| A | Existing launch tests green; new test for the timeout→poll path. | All 59 pre-existing launch tests still pass; 6 new F541 tests added. | see Test Results |
| B | Cold-boot launch test on a grok box (2026-08-28 directive). | NOT run here (laptop; box e2e is a separate step per the box-ops directive). Flagged as deferred. | — |

Notes on correctness:
- `requests.exceptions.ReadTimeout` subclasses `Timeout` subclasses
  `RequestException`, so the new `except Timeout` is ordered **before** the outer
  `except RequestException` and only intercepts timeouts; a plain
  `ConnectionError` on the initial POST still falls through to
  `"Failed to connect to cao-server"` (verified by the untouched
  `test_launch_request_exception`).
- When `--session-name` is omitted the server mints the name and only returns it
  in the POST response we never received, so the poll cannot address the session;
  the helper returns `None` and the caller raises the "could not be confirmed"
  error rather than fabricating success.

---

## F542 (#398) — terminal rows of a dead tmux session are never reconciled; poller logs Session-not-found forever

Root cause per the issue: the tmux server does not survive a host reboot, but the
DB rows do; the pipe-pane liveness watchdog re-arms a FIFO reader per surviving
row and probes `get_history` every few seconds, which logs
`clients.tmux - ERROR - Failed to get history from <session>:<window>` forever.

### Acceptance-criteria checklist

| # | AC (from #398) | How met | Evidence (file:line) |
|---|---|---|---|
| 1 | At server startup, reconcile terminal rows whose `tmux_session` does not exist: mark them terminated / run the cleanup path once, stop polling them. | New `reconcile_dead_session_terminals()` iterates DB rows, checks `backend.session_exists(session)` once per distinct session, and for an absent session runs the cleanup/terminated path once (`cleanup_provider` → `delete_terminal_and_warm_intent(preserve_warm_intent=False)` → `settle_pending_orphan_messages`). It is wired into the lifespan **between** `purge_stale_terminal_records` and `rearm_fifo_readers_at_startup`, so a dead-session row is reconciled instead of re-enrolled in the watchdog — which is what "stops polling them". | function def `services/terminal_service.py:897`; per-session existence probe `:939`; cleanup+delete+settle `:960`+ (mirrors the purge finalization at `:876`); lifespan wiring `api/main.py:1679` (after purge `:1666`, before re-arm `:1691`); log `startup_dead_session_reconcile` `api/main.py:1684` |
| 1b | (and on first poll miss) | STARTUP reconciler implemented as the primary storm-stopper. In addition, `tmux.get_history` no longer logs at ERROR for a genuinely-gone session/window `ValueError` (expected control flow the watchdog already classifies): the two known "gone" shapes are logged at DEBUG, so the per-tick ERROR storm named in the incident is silenced even during the watchdog's bounded probe window. The dedicated single-tick watchdog hook is DEFERRED (see below). | `clients/tmux.py:1297` (`except ValueError as e:`), DEBUG branch `:1310`, ERROR retained for other ValueErrors `:1312` and non-ValueError `:1315` |
| 2 | Idempotent; must not touch rows whose session exists. | A reconciled row is deleted, so a second sweep finds nothing (no-op). Rows whose `session_exists` is True are counted `skipped_session_live` and never touched. An **unclassifiable** `session_exists` failure is treated as "present" (`session_alive=True`) so a flaky backend never causes a false reconcile. | live-row skip `services/terminal_service.py:950`; existence cached per session `:939`; exception→leave-intact `:944`; herdr early-return `:922` |
| 3 | Unit test: row with nonexistent tmux session → reconciled once, no repeated poll errors. | `test/services/test_f542_dead_session_reconcile.py` — 7 tests incl. reconciled-once, idempotent-second-call-noop, live-never-touched, one-probe-per-session, mixed live/dead, session_exists-error-leaves-intact, herdr-skipped. | see Test Results |
| 4 | Box e2e: kill tmux server under a running cao-server, restart, journal quiet. | NOT run here (box e2e is a separate step). Flagged as deferred; reproduction recipe below. | — |

---

## Files touched

F541 (commit `a2f446a1`):
- `src/cli_agent_orchestrator/cli/commands/launch.py`
- `test/cli/commands/test_launch.py`

F542 (commit `e2406a47`):
- `src/cli_agent_orchestrator/services/terminal_service.py`
- `src/cli_agent_orchestrator/api/main.py`
- `src/cli_agent_orchestrator/clients/tmux.py`
- `test/services/test_f542_dead_session_reconcile.py` (new)

No edits to tests I did not write (a contract change was not required).

---

## Test results (exact commands + verbatim summary lines)

```
$ uv run pytest test/cli/commands/test_launch.py test/services/test_f542_dead_session_reconcile.py -q
66 passed in 5.96s
```
(59 pre-existing launch tests + 6 new F541 tests + 1 skip? no — 59 launch incl. 6 new, plus 7 F542 = 66.)

```
$ uv run pytest test/services/test_f542_dead_session_reconcile.py -q
7 passed in 2.92s
```

```
$ uv run pytest test/clients/test_tmux_client.py -q
92 passed in 2.22s
```

Broad regression sweep over everything my changes could affect (one deselect —
see below):
```
$ uv run pytest test/cli test/services/test_f542_dead_session_reconcile.py \
    test/clients/test_tmux_client.py test/api/test_plugin_lifespan.py \
    test/services/test_f202_fork_companions.py -q \
    --deselect test/cli/commands/test_config_reconcile.py::test_root_installer_delegates_without_toml_or_stanza_parsing
875 passed, 6 skipped in 28.04s
```

Per the laptop-RAM ban, the FULL pytest suite was NOT run — only touched files
plus `test/cli`.

### Deselected / pre-existing failure (NOT introduced by this work)
`test/cli/commands/test_config_reconcile.py::test_root_installer_delegates_without_toml_or_stanza_parsing`
fails with `FileNotFoundError: .../install.sh`. Cause: the test resolves
`ROOT_REPO` to the source tree and expects `install.sh` at the repo root, which a
git **worktree** does not have (`install.sh` exists only in the shared checkout).
Environmental, unrelated to F541/F542.

---

## mypy / isort / black baseline notes

- **black** (`uv run black --line-length 100 --check <touched files>`): clean —
  "6 files would be left unchanged."
- **mypy** (project `mypy.ini`, not `--strict` — the repo config sets
  `disallow_untyped_defs = False`): my added lines are error-free. `mypy` reports
  57 pre-existing errors in `terminal_service.py`/`main.py`, ALL outside my edited
  ranges (reported at lines 2479+, 2749, 2865, …; my F542 additions are
  `terminal_service.py:897-991` and `main.py:1679-1686`). `clients/tmux.py`
  reports 0 errors. The `launch.py:78/488` output is the informational
  "untyped function bodies not checked" note, consistent with the file's existing
  style (`launch()` itself is untyped).
- **isort** (`--profile black --line-length 100`): my diffs add NO new imports.
  isort flags ONE pre-existing local-import pair,
  `terminal_service.py:1480-1481` (`config_service` before `cc_session_registry`),
  verified byte-identical in base `f6c35046` — pre-existing baseline noise in a
  function I did not touch. I reverted isort's reorder of it to keep my diff
  scoped; it can be fixed separately if a fully isort-clean file is desired.

---

## r2 addendum — F541: claude_code cold-start init timeout (commit `97260c50`)

**Motivation.** grok-box-3 e2e reproduced a server 500
`Claude Code initialization timed out after 60s` (session rolled back) on this
branch. Investigation (message 1435) established the cause is NOT the F541/F542
diff but the UNCHANGED 60s global `provider_init_timeout`: Claude Code cold
start renders a ready status past 60s, so `create_terminal` tore a
genuinely-healthy launch down. Supervisor classified this as the SAME defect as
F541 (a cold launch killed by a timeout unrelated to real latency) and directed
a fix.

### Acceptance-criteria checklist (r2)

| # | AC (from #397, r2 directive) | How met | Evidence (file:line) |
|---|---|---|---|
| 1 | Raise the default init timeout for claude_code cold start. | claude_code now resolves a 180s default init cap instead of the 60s global. | new setting `claude_code_init_timeout: 180` `services/settings_service.py:244`; consumed by `ClaudeCodeProvider.get_init_timeout` `providers/claude_code.py:360-383` |
| 2 | Prefer a claude_code-specific override so OTHER providers keep 60s (cite the per-provider mechanism). | The existing per-provider mechanism is `BaseProvider.get_init_timeout(profile)` (`providers/base.py:853`), which resolves a per-profile `AgentProfile.provider_init_timeout` override else the global setting. I extended it the idiomatic way: a provider-class override. `ClaudeCodeProvider.get_init_timeout` resolves profile-override → `claude_code_init_timeout` setting → `super()` (global). Only claude_code is changed; every other provider still hits `BaseProvider.get_init_timeout` → global 60s. | override `providers/claude_code.py:360-383`; base unchanged `providers/base.py:853-869`; global default still `provider_init_timeout: 60` `services/settings_service.py:237` |
| 3 | Keep the knob user-settable. | `claude_code_init_timeout` is in `_SERVER_DEFAULTS` + `_SERVER_ENV_VARS` (env `CAO_CLAUDE_CODE_INIT_TIMEOUT`) and documented in `get_server_settings`. Settable via settings.json `server` block or the env var, same precedence as every other server setting (env > settings.json > default). A per-profile `provider_init_timeout` still overrides it. | default `services/settings_service.py:244`; env map `:268`; docstring `:289-295` |
| 4 | Add/adjust the unit test asserting the resolved default. | New `TestClaudeCodeInitTimeout` (5 tests) asserts claude_code default=180, profile-without-override=180, per-profile override wins (300), user-settable (90), fallback to global when key absent (60). New settings tests assert default=180 and global stays 60, and the knob is settable (240). The pre-existing exact-defaults assertion in `test_returns_defaults_when_no_settings` was updated to include the new key (legitimate contract change). | `test/providers/test_claude_code_unit.py::TestClaudeCodeInitTimeout`; `test/services/test_settings_service.py::TestGetServerSettings::{test_returns_defaults_when_no_settings,test_claude_code_init_timeout_default_is_180,test_claude_code_init_timeout_is_user_settable}` |
| 5 | CLI poll window >= server init timeout + margin (set to init_timeout+60 or flat 240). | `SESSION_START_POLL_TIMEOUT_S` and the POST read timeout `SESSION_START_TIMEOUT_S` both raised 120 → 240 (= claude_code ceiling 180 + 60s margin). Kept a flat constant (no extra round trip to read server settings before contacting the server). New test asserts both are >= 180 + 60. | `cli/commands/launch.py:72` (`SESSION_START_TIMEOUT_S = 240`), `:76` (`SESSION_START_POLL_TIMEOUT_S = 240`); rationale comment `:58-71`; test `test/cli/commands/test_launch.py::test_launch_poll_window_covers_server_init_ceiling` |

### Design note (why the provider override, and the lazy-import subtlety)
- The idiomatic per-provider seam here IS a `get_init_timeout` override — base
  already centralises the resolution and claude_code already calls
  `self.get_init_timeout(profile)` at `providers/claude_code.py:1053`. No new
  wiring needed at the call sites.
- The override lazy-imports `get_server_settings` INSIDE the method (mirroring
  `BaseProvider.get_init_timeout`) rather than using the module-top import.
  This is load-bearing: the existing `test_provider_init_timeout.py` suite
  patches `settings_service.get_server_settings`, and a module-top bound name
  would bypass that patch. With the lazy import, those tests still pass — when
  their mock omits the `claude_code_init_timeout` key, the override falls
  through to `super()` and returns the mocked global, preserving the base
  contract they assert.

### r2 test results (exact commands + verbatim summary lines)
```
$ uv run pytest test/providers/test_claude_code_unit.py::TestClaudeCodeInitTimeout \
    test/services/test_settings_service.py test/cli/commands/test_launch.py -q
151 passed in 11.12s
```
Broad regression sweep across everything r2 could affect (one deselect —
pre-existing worktree-only install.sh test):
```
$ uv run pytest test/cli test/services/test_settings_service.py \
    test/providers/test_claude_code_unit.py test/providers/test_provider_init_timeout.py \
    test/providers/test_base_provider.py test/providers/test_container_wrapped.py \
    test/services/test_f542_dead_session_reconcile.py test/clients/test_tmux_client.py -q \
    --deselect test/cli/commands/test_config_reconcile.py::test_root_installer_delegates_without_toml_or_stanza_parsing
1191 passed, 6 skipped in 39.88s
```

### r2 lint/type notes
- black: clean (6 touched files unchanged after format).
- isort: no new top-level imports in the touched source files (the override's
  `get_server_settings` is a lazy in-function import).
- mypy: my additions are error-free. `settings_service.py:866` reports one
  pre-existing `no-any-return` in the UNRELATED `ensure_script` TUI helper
  (present on base `f6c35046` at line 851, shifted by my added lines) — not
  introduced by r2. `claude_code.py` / `launch.py` additions are clean.

### r2 reviewer verification (detached worktree)
```bash
# claude_code resolves 180 by default; other providers keep 60:
uv run python - <<'PY'
from unittest.mock import patch
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.base import BaseProvider
S = "cli_agent_orchestrator.services.settings_service.get_server_settings"
with patch(S, return_value={"provider_init_timeout": 60, "claude_code_init_timeout": 180}):
    cc = ClaudeCodeProvider("t1", "s", "w")
    print("claude_code default:", cc.get_init_timeout())          # 180
    assert cc.get_init_timeout() == 180
# a concrete non-claude provider (base resolution) stays on the 60s global:
from cli_agent_orchestrator.models.agent_profile import AgentProfile
with patch(S, return_value={"provider_init_timeout": 60, "claude_code_init_timeout": 180}):
    # per-profile override still wins for claude_code:
    assert cc.get_init_timeout(AgentProfile(name="a", description="d",
                                            provider_init_timeout=300)) == 300
print("r2 init-timeout resolution OK")
PY
# NOTE: the *unmocked* defaults depend on any local settings.json `server`
# override, so assert the built-in defaults directly instead (env-independent):
uv run python -c "from cli_agent_orchestrator.services.settings_service import _SERVER_DEFAULTS as d; print(d['claude_code_init_timeout'], d['provider_init_timeout']); assert d['claude_code_init_timeout']==180 and d['provider_init_timeout']==60"
```

---

## r3 addendum — F548: init dialog handling + fast auth-fail + pane tail (commit `0ca9c2d0`, issue #404)

**Motivation.** The r2 box e2e still 500'd (`init timed out after 180s`,
never idle). The tester's discriminator (pane sampling every 15s + cold
`claude -p`, `probes/f541-diag.md`) found the real cause: on a fresh host the
first dialog is the **workspace-trust prompt whose focus DEFAULTS to
`❯ No, exit`** (with "Yes, I trust this folder" as the second row). The old
trust handler answered it with a **bare Enter** → confirmed "No, exit" → Claude
exited to the shell → the seat never reached a REPL → `wait_until_status` waited
out the full init timeout. A second, independent box blocker: `claude -p`
returns `Failed to authenticate: OAuth session expired…` (stale copied auth), so
even a correct trust-Yes would park on a login screen and never go idle.
Confirmed present on `main` too — this is why the box e2e could not pass
regardless of the F541 timeout raise.

### Acceptance-criteria checklist (r3)

| # | AC (from #404) | How met | Evidence (file:line) |
|---|---|---|---|
| 1a | Dialog handling keys on dialog TEXT; trust prompt → accept. | The trust branch keys on `TRUST_PROMPT_PATTERN` ("Yes, I trust this folder") and now selects the AFFIRMATIVE row: `Down` (off the default "No, exit") then `Enter`, mirroring the bypass handler — never a bare Enter. | trust branch `providers/claude_code.py:1140`; Down+Enter sequence `:1144-1166` |
| 1b | Bypass-permissions ack → select "Yes, I accept" (Down/2 + Enter). | Unchanged and already correct: the bypass branch sends `Down` then `Enter` for "Yes, I accept". The box's specific dialog is the TRUST prompt (probe), which now uses the same mechanic. Verified the Ink menu from the tester's pane samples: two rows, affirmative reached by one `Down`. | bypass branch `providers/claude_code.py:1073-1096` (pre-existing); trust now matches |
| 1c | Any other early dialog → do NOT blind-Enter; set WAITING_USER_ANSWER so the auto-responder owns it. | The startup loop only ever sends keys inside the bypass / external-import / trust TEXT branches; an unrecognised dialog falls through and is never Entered. `get_status` classifies an unrecognised choice dialog as WAITING_USER_ANSWER via `WAITING_USER_ANSWER_PATTERN`, and `initialize()`'s `wait_until_status` already accepts WAITING_USER_ANSWER, returning control to the auto-responder path. | loop key-sends are text-gated `providers/claude_code.py:1073-1166`; WAITING_USER_ANSWER pattern `:187`; init accepts it `:1230-1235` |
| 2 | Auth-failure/login screens during init → fail FAST with a named error (E-CLAUDE-AUTH). | New `ClaudeAuthError(ProviderError)` with `code = "E-CLAUDE-AUTH"`. `CLAUDE_AUTH_FAILURE_PATTERNS` (Failed to authenticate / OAuth session expired / /login / paste-code / login URL / Authentication required) → `_detect_claude_auth_failure`. Checked at the TOP of the startup-prompt loop (fail fast, before waiting) AND in the `initialize()` timeout branch (a login screen that renders after the loop's idle-gap exit). Message is prefixed `[E-CLAUDE-AUTH]` and names box-ops remediation. | class `providers/claude_code.py:78-89`; patterns `:214-224`; detector `:242-249`; loop fast-fail `:1059-1070`; timeout-branch check `:1257-1266` |
| 3 | Init-timeout / init-failure error text includes the last ~15 non-blank pane lines. | `_pane_tail(output, n=15)` (ANSI-stripped, last N non-blank lines). Both the `TimeoutError` and the `ClaudeAuthError` messages append `Last pane lines:\n{_pane_tail(...)}`. This flows into `create_terminal`'s `except Exception as e: "Failed to create terminal: {e}"` → the HTTP 500 body/log. | helper `providers/claude_code.py:227-239`; timeout raise `:1285-1289`; auth raise `:1065-1070`,`:1261-1266`; create surfaces it `services/terminal_service.py:2927-2929` |
| — | Keep the 180s default. | `claude_code_init_timeout` default unchanged at 180 (F541 r2). | `services/settings_service.py:244` |
| — | Box auth refresh is ops, not code. | Acknowledged — the stale-OAuth box needs `scripts/box-setup.sh` re-sync; E-CLAUDE-AUTH makes that failure fast and self-explaining instead of a 180s hang. | — |

### Design notes (r3)
- The trust branch now uses `asyncio.sleep` between Down and Enter (not a
  blocking `time.sleep`), consistent with the bypass branch and the
  single-event-loop async-offload doctrine (#451). One pre-existing coverage
  test (`test_trust_prompt_detected`) relied on the old blocking `time.sleep`
  to advance wall-clock for its idle-gap exit; it was reworked to drive the
  exit via a mocked `time.monotonic` sequence instead. This is the only test
  behavioural-model change; the assertion now verifies Down+Enter.
- `ClaudeAuthError` subclasses the module's `ProviderError` (an `Exception`),
  so it flows through `create_terminal`'s generic failure path and becomes the
  500 detail with its `[E-CLAUDE-AUTH]` message + pane tail — no new special
  re-raise wiring was required. (The pre-existing `ProviderAuthRefreshFailed`
  special-case at `terminal_service.py:2808` is left as-is; E-CLAUDE-AUTH is a
  distinct, init-time, pane-derived signal.)

### Contract changes to pre-existing tests (r3)
The trust prompt changing from bare-Enter to Down+Enter is a legitimate
behavioural contract change. Six pre-existing tests asserting the old contract
were updated to the new one (send_keys Down count +1 per trust dialog):
- `test/providers/test_claude_code_unit.py`: `test_handle_bypass_then_trust_prompt`,
  `test_handle_trust_then_external_import_prompt_rejects_import`
- `test/providers/test_claude_code_coverage.py`: `test_trust_prompt_detected`
  (also reworked to mock `time.monotonic`, see design note)
- `test/providers/test_startup_prompt_idle_gap.py`: `test_late_prompt_handled`,
  `test_cascading_prompts_all_handled`, `test_idle_gap_resets_on_each_prompt`
  (the ClaudeCode class only — Kimi/Antigravity classes untouched)
- `test/providers/test_container_wrapped.py`: `test_idle_timeout_prompt_handler`

### r3 test results (exact commands + verbatim summary lines)
```
$ uv run pytest test/providers/test_claude_code_unit.py::TestF548InitDialogAndAuth -q -n0
6 passed in 1.73s
```
```
$ uv run pytest test/providers/test_claude_code_unit.py test/providers/test_claude_code_coverage.py \
    test/providers/test_startup_prompt_idle_gap.py test/providers/test_container_wrapped.py \
    test/providers/test_provider_init_timeout.py test/providers/test_base_provider.py -q -n0
273 passed in 39.37s
```
Broad provider + CLI sweep (serial, `-n0`):
```
$ uv run pytest test/providers test/cli test/services/test_settings_service.py \
    test/services/test_f542_dead_session_reconcile.py -q -n0
2924 passed, 19 skipped, 6 xfailed, 1 xpassed  (2 pre-existing failures — see below)
```

### r3 lint/type notes
- black: clean (5 touched files).
- isort: clean on `claude_code.py`; the only new import is `call` added to the
  existing `unittest.mock` import in `test_claude_code_unit.py`.
- mypy: `Success: no issues found` on `claude_code.py`.

### r3 pre-existing / environmental failures (NOT introduced by F548)
My r3 diff touches ONLY `providers/claude_code.py` + its 4 provider test files.
Two failures in the broad sweep are unrelated:
1. `test/cli/commands/test_config_reconcile.py::test_root_installer_delegates…`
   — the known worktree-only `install.sh` FileNotFoundError (git worktrees have
   no `install.sh` at root).
2. `test/providers/test_omp_unit.py::test_extension_root_merges_mcp_without_overriding_explicit_terminal_id`
   — the OMP provider merges an extra `CAO_TERMINAL_TOKEN` env key; this leaks
   from THIS worker's own CAO environment into the test's expected-env
   assertion. OMP is not touched by F548 (or any of my commits). Environmental.

### r3 reviewer verification (detached worktree)
```bash
# Trust dialog: affirmative selection is Down+Enter, never a bare Enter.
uv run pytest test/providers/test_claude_code_unit.py::TestF548InitDialogAndAuth -q -n0   # 6 passed
# Direct unit exercise of the two helpers + the fast-fail:
uv run python - <<'PY'
from cli_agent_orchestrator.providers.claude_code import (
    _detect_claude_auth_failure, _pane_tail, ClaudeAuthError,
)
assert _detect_claude_auth_failure("Failed to authenticate: OAuth ...")
assert _detect_claude_auth_failure("OAuth session expired")
assert _detect_claude_auth_failure("Welcome to Claude Code v2.1.248") is None
assert _pane_tail("a\n\nb\n  \nc", n=2).splitlines() == ["b", "c"]
assert ClaudeAuthError.code == "E-CLAUDE-AUTH"
print("F548 helpers OK")
PY
```

---

## r4 addendum — F548 gate SHOULD-1: land the timeout-branch regression test (commit `60f4cf37`, issue #404)

**Context.** The empirical delta gate on `b46bf972` ruled **GATE-YES (0 BLOCKER
/ 1 SHOULD / 2 NIT)** (codex `cf2a83bc`, report
`/data/cao-scratch/fork-gate-report-f548.md`, artifact SHA
`0b5c4396…`). This r4 lands the single SHOULD; the two NITs are noted below.

### SHOULD-1 — timeout-branch E-CLAUDE-AUTH had no committed regression test
The gate found that the `initialize()` timeout-branch auth classifier
(`claude_code.py:1257-1266` → `ClaudeAuthError`) was correct and passed the
reviewer's independent repro, but a scratch mutation **deleting** it survived
all four touched test files (224 passed) — i.e. no committed test would catch a
future refactor that dropped it.

**Resolution (attachment-landing doctrine: land as-is).** The reviewer
attachment `orchestrator/tmp/orch/f548-gate-attachments/test_f548_gate_repro.py`
was landed **VERBATIM** (byte-for-byte, `diff -q` identical) into the fork test
tree at `test/providers/test_f548_gate_repro.py`. No amendments were made — the
doctrine permits amending only with a stated rationale in this report, and none
was needed: the file is already black-clean (`black --check`: would be left
unchanged) and passes as-is. Its 9 tests cover:
- each required auth shape detected independently (`Failed to authenticate`,
  `OAuth session expired`, `Paste code here`, `Paste the code here`,
  `Visit the following URL to log in`, `Visit https://claude.ai/login…`);
- **the timeout branch** — `test_initialize_timeout_branch_raises_named_auth_with_real_tail`
  stubs the startup handler, forces `wait_until_status → False`, supplies an
  auth pane with unique `sentinel prelude`/`sentinel trailer` lines, and asserts
  `ClaudeAuthError.code == "E-CLAUDE-AUTH"` plus both unique pane-tail lines;
- the unrecognized-choice → `WAITING_USER_ANSWER` / no-blind-Enter halves.

**Mutation verification (the point of SHOULD-1).** Performed locally on an
overlay of the reviewed source and then reverted exactly (final `git status`
shows only the new untracked test; `claude_code.py` unmodified):

| State | `test/providers/test_f548_gate_repro.py -n0` |
|---|---|
| classifier present (as committed) | **9 passed in 0.53s** |
| timeout-branch classifier removed from `claude_code.py` | **1 failed, 8 passed** — `test_initialize_timeout_branch_raises_named_auth_with_real_tail` fails, raising `TimeoutError` (`…timed out after 180s. Last pane lines: … Failed to authenticate …`) instead of `ClaudeAuthError` |

So the landed test **kills the exact mutation** that survived before (the
mandated four touched files stayed green under that mutation; this file does
not). SHOULD-1 is discharged.

### NITs (recorded; not addressed under the frozen-pin discipline)
- **NIT-1** — the r3 section said "Six pre-existing tests" but enumerated
  **seven** (2 unit + 1 coverage + 3 idle-gap + 1 container-wrapped). Correcting
  the count here for the record: **seven** pre-existing tests were updated for
  the trust bare-Enter → Down+Enter contract. No contract weakening (the gate
  confirmed the coverage test's removed `mock_sleep.await_count <= 20` is
  replaced by a finite `time.monotonic.side_effect`, which still enforces the
  bounded-settle invariant).
- **NIT-2** — the trust-prompt docstring at `claude_code.py:986-988` still reads
  "requires Enter". Left unchanged to keep this commit scoped to landing the
  regression test; flagged for a follow-up prose fix (a one-line docstring edit)
  if the supervisor wants it in this branch.

### r4 test results (exact commands + verbatim summary lines)
```
$ uv run pytest test/providers/test_f548_gate_repro.py -q -n0
9 passed in 0.53s
```
Mandated serial touched set + the new file together:
```
$ uv run pytest test/providers/test_claude_code_coverage.py \
    test/providers/test_claude_code_unit.py test/providers/test_container_wrapped.py \
    test/providers/test_startup_prompt_idle_gap.py test/providers/test_f548_gate_repro.py -q -n0
233 passed in 38.87s
```

### r4 integrity
- Landed file is byte-identical to the attachment (`diff -q` clean).
- Mutation was applied to and reverted from the worktree cleanly; post-revert
  `git status --short` showed only the new untracked test file, then it was
  committed as `60f4cf37`.
- No source (`claude_code.py`) change in this commit — regression test only.

---

## r5 addendum — F548 box e2e r3 fix: real Down key + settle poll + structured errors (commit `c5587aa4`, issue #404)

**Box e2e r3 @ `61008d9d` FAILED both legs on grok-box-2.** Evidence:
`/data/cao-scratch/f548-e2e-r3/` and `orchestrator/tmp/orch/f548-e2e-r3/`.

### What I disproved / proved (with evidence)
- **Hypothesis 1 (the arrow is sent as literal bytes, not a real key) — PROVEN, this is the cause.**
  `pos.native.log`: `10:19:55,214 … selecting 'Yes, I trust this folder'` →
  `10:19:55,215 … send_keys … keys length: 3` (the 3 bytes `\x1b[B`) →
  `10:19:56,032 … send_special_key … key: Enter`. The final pane in the timeout
  error still shows `❯ No, exit` above `box@cursor:~/cli-subagents$` — Claude
  EXITED. `send_keys` uses tmux load-buffer/paste-buffer (vis-sanitized on
  tmux ≥ 3.7), so `\x1b[B` is delivered as LITERAL TEXT; Ink never parses it as
  Down, focus stayed on "No, exit", and Enter confirmed EXIT.
  **Fix:** send the arrow as a tmux SPECIAL KEY — `send_special_key("Down")`
  (libtmux `send_keys("Down", enter=False)` → the real key name, `clients/tmux.py:1214`).
- **Hypothesis 2 (keys fire before Ink's input handler attached) — mitigated by the settle poll.**
  After the real Down, `_select_menu_affirmative` re-captures the pane and waits
  until `❯` sits on the affirmative row before Enter, giving Ink time to attach.
- **Hypothesis 3 (repaint resets focus) — mitigated by the same settle poll**, which
  re-reads after any transient repaint rather than trusting a single frame.
- **Negative leg (creds moved): the auth screen was masked by the trust exit.**
  With trust now actually accepted, the startup loop's next iteration re-reads
  the post-Enter pane and the top-of-loop auth check catches a login screen,
  failing fast as E-CLAUDE-AUTH (see r3 section). Also confirmed the P/N legs'
  transport defect: `pos.headers` = `HTTP/1.1 500 … content-type: text/plain`
  with body `Internal Server Error`; `neg.headers` empty (hang) — no structured
  error. Fixed below.

### Acceptance-criteria checklist (r5)

| # | Directive | How met | Evidence (file:line) |
|---|---|---|---|
| 1 | Send Down as a tmux special-key name, not literal ESC[B. | `_select_menu_affirmative` sends `send_special_key("Down")`; all three dialogs (bypass/trust/external-import) route through it; no `send_keys("\x1b[B")` remains. | helper `providers/claude_code.py:975`; Down send `:1020`; call sites `:1157`,`:1178`,`:1221` |
| 2 | Add a settle poll: re-capture pane, confirm focus moved to the affirmative before Enter. | The helper checks the caller's current pane first, then re-reads until `❯` precedes the affirmative option (`❯[^\n]*<affirmative>`), then sends Enter (after confirm-or-timeout; WARNING if unconfirmed). | helper body `providers/claude_code.py:975-1054` |
| 3 | Negative-leg: check auth AFTER trust accepted (pane after Enter). | The startup loop re-reads the pane every iteration and runs the auth fast-fail at the TOP, so the iteration after trust-accept inspects the post-Enter pane. | loop auth check (r3) `providers/claude_code.py` top of loop; loop re-reads each iteration |
| 4 | Pre-init failure must be a 4xx/5xx JSON with E-CLAUDE-AUTH, not text/plain 500 / hang. | `start_session_endpoint` maps `ClaudeAuthError` → 500 JSON `{code:'E-CLAUDE-AUTH', message}` and a provider-init `TimeoutError` → 500 JSON `{code:'provider_init_timeout', message}` (message carries the pane tail). | auth arm `api/main.py:3758`; timeout arm `:3763-3777`; import `api/main.py:129` |
| 5 | Unit tests: exact key sequence + settle poll; mutation (raw ESC[B) fails. | `test_trust_prompt_selects_affirmative_row_not_bare_enter` + `test_trust_prompt_down_is_special_key_not_literal_escape` assert `send_special_key('Down')` then `'Enter'` and NO `send_keys`. Mutation-verified: reverting the Down to `send_keys("\x1b[B")` fails BOTH (locally observed, reverted exactly). | `test/providers/test_claude_code_unit.py::TestF548InitDialogAndAuth` |
| 6 | Corrected negative-leg contract. | Two endpoint tests assert the structured JSON (E-CLAUDE-AUTH; provider_init_timeout) and `content-type: application/json` (not text/plain). | `test/api/test_wp2s3_http_contract.py::{test_start_claude_auth_error_is_structured_json_e_claude_auth,test_start_init_timeout_is_structured_json_not_text_plain}` |
| 7 | Box-runnable repro note. | Below. | — |

### Design note (why Enter is still sent when focus is unconfirmed)
The r3-box failure was NOT "blind Enter after a real Down moved focus" — it was
a literal-bytes arrow that never moved focus at all. With a real Down key, focus
moves. The settle poll's job is (a) give Ink time to attach and (b) verify. If a
flaky/slow capture can't confirm within `settle_timeout`, refusing to Enter would
strand every launch; so the helper Enters anyway and logs a WARNING. The
confirm/return value gates tests and observability, not the keypress.

### Contract change to pre-existing tests (r5)
Seven pre-existing bypass/trust/idle-gap/container tests asserted the old
`send_keys("\x1b[B")` arrow. They were updated to the new real-special-key
contract (Down+Enter via `send_special_key`, no `send_keys`), with fixtures that
model `❯` moving onto the affirmative row so the settle poll confirms:
`test/providers/test_claude_code_unit.py` (2), `test_claude_code_coverage.py` (3:
trust/bypass/external-import), `test_startup_prompt_idle_gap.py` (5: the
ClaudeCode class only — Kimi/Antigravity untouched), `test_container_wrapped.py`
(2).

### r5 test results (exact commands + verbatim summary lines)
```
$ uv run pytest test/providers/test_claude_code_unit.py test/providers/test_claude_code_coverage.py \
    test/providers/test_startup_prompt_idle_gap.py test/providers/test_container_wrapped.py \
    test/providers/test_f548_gate_repro.py test/api/test_wp2s3_http_contract.py -q -n0
253 passed
```
Mutation verification (raw ESC[B via send_keys), reverted exactly afterward:
```
test_trust_prompt_down_is_special_key_not_literal_escape       FAILED (down_calls == 0)
test_trust_prompt_selects_affirmative_row_not_bare_enter       FAILED (special-key seq lacks 'Down')
```

### r5 lint/type
- black: `claude_code.py`, `main.py`, and my added test blocks are clean.
  (`test/api/test_wp2s3_http_contract.py` was already non-black-compliant on the
  pinned HEAD; I appended only black-clean tests and did NOT reformat the
  pre-existing body, to avoid unrelated churn — `black --diff` shows no change
  to my added block.)
- isort: clean on `claude_code.py` / `main.py` (the new `ClaudeAuthError` import
  in main.py is ordered).
- mypy: `claude_code.py` clean; `main.py` has only the two pre-existing errors
  (lines 7760, 8324) outside my edits.

### r5 box-runnable repro note (grok box, tmux backend)
Prereq: a box with valid Claude auth (else you exercise the E-CLAUDE-AUTH leg,
which is also correct — it should now 500 fast with a JSON `E-CLAUDE-AUTH` body,
not hang). Build the fork at this HEAD and launch cold:
```bash
# On the box, in the fork checkout at c5587aa4 (or cao/e08be272 tip):
uv tool install --force --python 3.14 .
export CAO_HOME_DIR=/data/cao-scratch/f548-e2e/cao-home   # isolated
cao-server >/data/cao-scratch/f548-e2e/server.stdout 2>&1 &
# cold launch immediately (fresh host => trust dialog focuses 'No, exit'):
cao launch --agents chao_supervisor --provider claude_code \
  --session-name f548chk --headless --yolo
```
Expected with this fix + valid auth:
- server log shows `Workspace trust prompt detected, selecting 'Yes, I trust
  this folder'`, then `send_special_key … key: Down`, then a settle confirm
  (`affirmative row confirmed focused; sending Enter`), then `Enter`;
- the pane advances PAST the trust dialog to the Claude REPL; `wait_until_status`
  reaches idle; `cao launch` exits 0 and `tmux attach -t cao-f548chk` shows the
  REPL (NOT a shell prompt).
Failure signatures this fixes:
- OLD: `send_keys … keys length: 3` then Enter, pane stuck on `❯ No, exit`,
  180s timeout, `HTTP/1.1 500 … text/plain Internal Server Error`.
- If auth is stale on the box: expect a FAST (not 180s) `HTTP/1.1 500` with
  `content-type: application/json` and body `{"detail":{"code":"E-CLAUDE-AUTH",
  "message":"[E-CLAUDE-AUTH] …\nLast pane lines:\n…"}}` — re-sync box auth
  (`scripts/box-setup.sh`), an ops step, then re-run.
Auth-only quick check (no CAO): `claude -p` on the box; rc=1 +
`Failed to authenticate: OAuth session expired …` means box auth must be
re-synced before a green e2e is possible.

---

## r6 addendum — F548: post-idle "Not logged in" auth scan on Claude ≥ 2.1.250 (commit `7f07e277`, issue #404)

**Box e2e r4** (`probes/f548-box-e2e-r4.md`, grok-box-5, Claude Code **2.1.250**,
HEAD `5d4c410d` which includes the r3 fix `c5587aa4`) found:
- **Positive/trust leg not exercised:** under `skipDangerousModePermissionPrompt:true`
  (rewritten unconditionally by `_ensure_skip_bypass_prompt_setting`) +
  `--dangerously-skip-permissions`, 2.1.250 rendered **no** workspace-trust
  dialog for an untrusted dir ("started without prompts"). The Down+Enter path
  was not triggered.
- **Negative leg reached the REPL:** credentials moved aside → Claude rendered
  an **unauthenticated REPL** showing `Not logged in · Run /login`, idled, and
  `POST /sessions/start` returned **HTTP 200 in 38s** — `ClaudeAuthError` never
  fired, because `wait_until_status` SUCCEEDED (readiness = banner+idle,
  regardless of auth), so neither the startup-loop nor the timeout-branch auth
  check ran.

### Plain statement on the trust-dialog path (r6)
On **Claude Code ≥ 2.1.250** with `skipDangerousModePermissionPrompt` +
`--dangerously-skip-permissions` (CAO's default launch shape), the **workspace-
trust dialog does not render**, so the F548 **Down+Enter selection path is
UNIT-VERIFIED ONLY** and is not reachable via a box e2e on that version. It
remains correct and necessary for versions/paths that DO render the dialog (r3's
box did). The box e2e covers the **negative (auth) leg**.

### What r6 changes (the "(2)" fix)

| # | Directive | How met | Evidence (file:line) |
|---|---|---|---|
| 1 | Add `Not logged in(?:\s*·\s*Run /login)?` to the auth patterns. | Added to `CLAUDE_AUTH_FAILURE_PATTERNS`. | `providers/claude_code.py:214-222` (new pattern at `:219`) |
| 2 | After `wait_until_status` succeeds, one pane scan; on match raise `ClaudeAuthError` with the pane tail (same classifier as the timeout branch). No other behavior change. | Post-idle `get_history` → `_detect_claude_auth_failure` → `ClaudeAuthError("[E-CLAUDE-AUTH] … reached its REPL unauthenticated … Last pane lines:\n…")`; only runs on the success path, only raises on a marker. | `providers/claude_code.py` post-idle block right after the `wait_until_status` success (immediately before `wait_until_input_ready`) |
| 3a | Test: post-idle logged-out pane → ClaudeAuthError; endpoint 500 JSON E-CLAUDE-AUTH. | `test_post_idle_logged_out_pane_raises_e_claude_auth` (unit, asserts code + pane tail); the endpoint JSON is covered by `test_start_claude_auth_error_is_structured_json_e_claude_auth`, which maps `ClaudeAuthError` **by type** regardless of which init site raised it. | `test/providers/test_claude_code_unit.py::TestF548InitDialogAndAuth`; `test/api/test_wp2s3_http_contract.py` |
| 3b | Test: normal idle pane → no raise. | `test_post_idle_authenticated_pane_does_not_raise` (init returns True). | same class |
| 3c | Mutation note: removing the post-idle scan fails (a). | Neutering the scan (`idle_auth_marker = None`) makes `test_post_idle_logged_out_pane_raises_e_claude_auth` fail with **"DID NOT RAISE ClaudeAuthError"** — verified locally, reverted exactly. | — |

Also: the detector unit now asserts `Not logged in · Run /login` (and bare
`Not logged in`) match. Two pre-existing success-path tests
(`test_initialize_calls_handle_startup_prompts`, `test_wrapped_provider_lifecycle`)
got ONE extra authenticated post-idle `get_history` frame for the new scan's
read — no assertion weakened.

### r6 test results (exact commands + verbatim summary lines)
```
$ uv run pytest test/providers/test_claude_code_unit.py::TestF548InitDialogAndAuth \
    test/api/test_wp2s3_http_contract.py test/providers/test_f548_gate_repro.py -q -n0
37 passed
$ uv run pytest test/providers/test_claude_code_unit.py test/providers/test_claude_code_coverage.py \
    test/providers/test_startup_prompt_idle_gap.py test/providers/test_container_wrapped.py \
    test/providers/test_f548_gate_repro.py test/api/test_wp2s3_http_contract.py -q -n0
255 passed
```
black/isort/mypy: `claude_code.py` clean (black unchanged, isort clean, mypy
"Success: no issues found").

### r6 box negative-leg repro recipe (Claude ≥ 2.1.250, tmux)
On a box whose Claude auth is currently VALID (so the failure is induced, not
pre-existing):
```bash
# fork checkout at 7f07e277 (cao/e08be272 tip), then:
uv tool install --force --python 3.14 .
export CAO_HOME_DIR=/data/cao-scratch/f548-neg/cao-home
cao-server >/data/cao-scratch/f548-neg/server.stdout 2>&1 &
# move real credentials aside, with a trap to restore no matter what:
CRED=~/.claude/.credentials.json
cp "$CRED" "$CRED.f548bak" && trap 'mv -f "$CRED.f548bak" "$CRED"' EXIT
mv "$CRED" "$CRED.moved"
cao launch --agents chao_supervisor --provider claude_code \
  --session-name f548neg --headless --yolo
# restore immediately (the trap also covers a crash):
mv -f "$CRED.moved" "$CRED"
```
Expected on 2.1.250 WITH r6: the seat reaches its REPL showing
`Not logged in · Run /login`, the post-idle scan fires, and
`POST /sessions/start` returns a **fast** `HTTP/1.1 500` with
`content-type: application/json` body
`{"detail":{"code":"E-CLAUDE-AUTH","message":"[E-CLAUDE-AUTH] … Not logged in … Last pane lines:\n…"}}`
(NOT the pre-r6 `HTTP 200` idle). Cleanup: the `trap`/`mv` restores
`.credentials.json`; kill the server and `tmux kill-server` when done.
Auth-only precheck: `claude -p` returns rc=1 (unauthenticated) while
credentials are moved.

---

## r7 addendum — F548 empirical gate r1 (0B/2S/1N) closed (commit `affe0438`, issue #404)

Empirical gate r1 (`orchestrator/tmp/orch/f548-empirical-gate-r1.md`, delta
`61008d9d..e2728529`) ruled GATE-NO **only** on the two BOX e2e legs being
unrunnable in that environment (whole grok fleet down / no Claude CLI / box
checkout guard) — NOT a change-merit failure. On merit it was **0 BLOCKING /
2 SHOULD / 1 NIT**. r7 closes all three.

| id | Gate finding | r7 resolution | Evidence (file:line) |
|---|---|---|---|
| S-1 | The "remove the settle-poll" mutation SURVIVED the whole test set — no committed test gated the settle/confirm behavior (the key sequence is unchanged because Enter is always sent). | Added two direct `_select_menu_affirmative` tests: (1) current pane + first re-read show `❯` on the NEGATIVE row, a LATER re-read shows it on the affirmative → asserts `confirmed is True` (reachable ONLY if the re-read poll runs) and Enter follows; (2) never-confirms → returns `False` + logs the WARNING. Mutation verified: neutering the poll (`if False and not confirmed`) makes test (1) fail `assert False is True` — done locally, reverted exactly. | `test/providers/test_claude_code_unit.py::TestF548InitDialogAndAuth::{test_settle_poll_confirms_only_after_affirmative_capture,test_settle_poll_warns_and_returns_false_when_never_confirms}` |
| S-2 | The post-idle auth scan `re.search`ed the whole 200-line `get_history` window → false-positive on an AUTHENTICATED pane merely containing "Not logged in" (reachable via `--resume` transcript replay). | Bounded the scan to the live footer: `get_history(..., tail_lines=_POST_IDLE_AUTH_SCAN_LINES=15)` AND detect over `_pane_tail(idle_pane, 15)` only. Tests: (a) authed pane with "Not logged in" ~40 lines up + clean footer → no raise; (b) marker in the tail → E-CLAUDE-AUTH. Mutation verified: scanning the full pane fails (a) — done locally, reverted. | constant `providers/claude_code.py` `_POST_IDLE_AUTH_SCAN_LINES` (near auth patterns); scan site (post-`wait_until_status` block); tests `::{test_post_idle_auth_scan_ignores_marker_far_up_scrollback,test_post_idle_auth_scan_raises_when_marker_in_tail}` |
| N-1 | Stale `_handle_startup_prompts` docstring said the workspace-trust dialog "requires Enter". | Docstring now states focus defaults to "❯ No, exit" with the affirmative second and requires Down+Enter via `_select_menu_affirmative`. | `providers/claude_code.py` `_handle_startup_prompts` docstring item 2 |

Note: the startup-LOOP auth check (inside `_handle_startup_prompts`) is left
scanning its per-iteration pane as before — it runs on the pre-REPL dialog
frames, not a replayed transcript, so S-2's `--resume` false-positive is
specific to the post-idle scan, which is the one bounded here.

### r7 test results (exact command + verbatim summary line)
```
$ uv run pytest test/providers/test_claude_code_unit.py test/providers/test_claude_code_coverage.py \
    test/providers/test_startup_prompt_idle_gap.py test/providers/test_container_wrapped.py \
    test/providers/test_f548_gate_repro.py test/api/test_wp2s3_http_contract.py -q -n0
259 passed
```
black/isort/mypy: `claude_code.py` clean (black would leave unchanged, isort
clean, mypy "Success: no issues found").

### r7 mutation ledger (both applied to the worktree then reverted exactly; final `git status` clean)
- settle-poll removed (`if False and not confirmed`) → `test_settle_poll_confirms_only_after_affirmative_capture` FAILS (`assert False is True`); the warns-path test still passes. (closes S-1's surviving mutation)
- post-idle scan over full pane (not `_pane_tail`) → `test_post_idle_auth_scan_ignores_marker_far_up_scrollback` FAILS (spurious ClaudeAuthError). (proves S-2 bound is load-bearing)

### r7 note on the box legs (items 4 & 5, still deferred)
The gate's GATE-NO was procedural: the box e2e could not run (fleet down / no
Claude CLI / box-checkout guard). That is unchanged and out of this lane's
control; r7 addresses only the change-merit SHOULDs/NIT. The r6 box
negative-leg recipe (above) still applies once a box with a valid Claude CLI is
available.

---

## r8 addendum — F548 gate r2 B-1 fixed: settle-poll re-read exhausted a 2-value test fixture (commit `a14faba7`, issue #404)

Empirical gate r2 ruled **GATE-NO / 1 BLOCKING**.

**B-1.** At `26878496`,
`test/providers/test_provider_init_timeout.py::TestStartupPromptHandlerHonorsOuterTimeout::test_passed_outer_timeout_extends_deadline_past_settings_default`
FAILED with `RuntimeError: coroutine raised StopIteration` (@ `claude_code.py`
in the settle-poll re-read). Cause: the r5 settle poll adds a `get_history`
re-read when the affirmative row is not already focused in the current pane;
that test's `get_history.side_effect` was a 2-value list whose trust frame
(`"Yes, I trust this folder"`, no `❯` on the affirmative) did NOT confirm from
`current_pane`, so the poll re-read past the end of the list.

**Fix (identical shape to the 7 tests adjusted in r6/r7):** focus the
affirmative row in the fixture — `"❯ Yes, I trust this folder"` — so
`_select_menu_affirmative` confirms from the current pane WITHOUT a re-read
(and, via the r7 short-circuit, without consuming an extra `time.monotonic`,
keeping the test's exact 5-value clock sequence valid). Then updated the stale
`send_special_key.assert_called_once()` to the F548 real-special-key contract
(`Down` then `Enter`; `send_keys` not called) and added `call` to the mock
import.

**Bug-family sweep (as mandated).** Audited every `_handle_startup_prompts` /
`_select_menu_affirmative` call site under `test/providers/` for `get_history`
`side_effect` lists of length ≤ 2 that enter the affirmative-selection re-read:
- `test_provider_init_timeout.py`: the failing test (fixed here); its sibling
  `test_passed_outer_timeout_caps_a_wedged_start` uses a non-dialog
  `return_value` (never enters `_select_menu_affirmative`) — safe.
- `test_claude_code_unit.py`, `test_claude_code_coverage.py`,
  `test_startup_prompt_idle_gap.py`, `test_container_wrapped.py`,
  `test_f548_gate_repro.py`: already handled in r6/r7 (affirmative-focused
  fixtures / stateful captures / stubbed helper). No remaining ≤2-length
  offender.

### r8 test result (exact command + verbatim summary line)
```
$ uv run pytest test/providers/test_provider_init_timeout.py test/providers -q -n0
1 failed, 2083 passed, 13 skipped, 6 xfailed, 1 xpassed
```
The **single failure is the PRE-EXISTING F550 (#406)**
`test_omp_unit.py::test_extension_root_merges_mcp_without_overriding_explicit_terminal_id`
— the OMP provider merges an extra `CAO_TERMINAL_TOKEN` that leaks from THIS
worker's own CAO environment into the test's expected-env assertion. OMP is not
touched by this branch; unrelated to F548/B-1. (B-1 itself: PASS.)
black/isort: touched test file clean; no source change this round.

---

## Deferred item

**F542 first-miss watchdog hook.** The issue wording is "at startup AND on first
poll miss". I implemented the startup reconciler + the `get_history` ERROR→DEBUG
downgrade (which removes the log storm on the watchdog's session-gone probe path).
I deliberately did NOT change the existing F138/F218 `_f138_definitive_absence`
2-hit threshold in `services/fifo_reader.py`, because altering that shared
confirmed-gone machinery risks breaking F138/F218 contracts/tests
(stop-and-ask territory). A dedicated single-tick reconcile hooked into the
watchdog session-gone path is deferred; supervisor is noting it on #398.

---

## Reviewer verification recipe (detached worktree)

An empirical reviewer can reproduce everything without touching the shared
checkout. Create a throwaway worktree at these commits:

```bash
# From any clone of the fork that has cao/e08be272 fetched:
git worktree add /tmp/rev-f541f542 cao/e08be272   # or a non-/tmp scratch dir
cd /tmp/rev-f541f542
git log --oneline -3        # expect e2406a47 (F542), a2f446a1 (F541), f6c35046 (base)
uv sync                     # builds the venv for this worktree
```

### 1. Automated tests
```bash
uv run pytest test/cli/commands/test_launch.py test/services/test_f542_dead_session_reconcile.py -q
uv run pytest test/clients/test_tmux_client.py -q
# Broad sweep (deselect the pre-existing worktree-only install.sh test):
uv run pytest test/cli test/services/test_f542_dead_session_reconcile.py \
  test/clients/test_tmux_client.py test/api/test_plugin_lifespan.py \
  test/services/test_f202_fork_companions.py -q \
  --deselect test/cli/commands/test_config_reconcile.py::test_root_installer_delegates_without_toml_or_stanza_parsing
```
Expected: `66 passed`, `92 passed`, and `875 passed, 6 skipped` respectively.

### 2. Lint/type baseline
```bash
uv run black --line-length 100 --check \
  src/cli_agent_orchestrator/cli/commands/launch.py \
  src/cli_agent_orchestrator/services/terminal_service.py \
  src/cli_agent_orchestrator/clients/tmux.py \
  src/cli_agent_orchestrator/api/main.py \
  test/cli/commands/test_launch.py \
  test/services/test_f542_dead_session_reconcile.py
uv run mypy src/cli_agent_orchestrator/cli/commands/launch.py \
  src/cli_agent_orchestrator/services/terminal_service.py \
  src/cli_agent_orchestrator/clients/tmux.py \
  src/cli_agent_orchestrator/api/main.py
```
Expected: black clean; mypy errors confined to pre-existing lines outside
`terminal_service.py:897-991` and `main.py:1679-1686` (tmux.py 0 errors).

### 3. Manual F541 repro (mock a slow `/sessions/start`) — no server needed
Drives the exact timeout→poll→success path with mocks (this is what the new
tests do; run it standalone to watch it live):
```bash
uv run python - <<'PY'
from unittest.mock import MagicMock, patch
import requests
from click.testing import CliRunner
from cli_agent_orchestrator.cli.commands.launch import launch, SESSION_START_TIMEOUT_S

runner = CliRunner()
with (
    patch("cli_agent_orchestrator.cli.commands.launch.requests.post") as post,
    patch("cli_agent_orchestrator.cli.commands.launch.requests.get") as get,
    patch("cli_agent_orchestrator.cli.commands.launch.time.sleep"),
):
    # POST is slow -> read timeout (NOT a connection failure)
    post.side_effect = requests.exceptions.ReadTimeout("read timeout=%d" % SESSION_START_TIMEOUT_S)
    # GET /sessions/<name> shows the supervisor terminal DID come up
    resp = MagicMock(); resp.status_code = 200
    resp.json.return_value = {
        "session": {"id": "claude-orch5"},
        "terminals": [{"id": "60d393b2", "name": "chao_supervisor",
                       "session_name": "cao-claude-orch5"}],
    }
    get.return_value = resp
    r = runner.invoke(launch, ["--agents", "chao_supervisor",
                               "--session-name", "claude-orch5", "--headless", "--yolo"])
    print("exit:", r.exit_code)           # expect 0
    print(r.output)                       # "still initializing... confirming" + "Terminal created: chao_supervisor"
    assert r.exit_code == 0
    assert "Failed to connect to cao-server" not in r.output   # NOT misreported
print("F541 confirm-then-attach OK")
PY
```
Also verify the negative path (supervisor never appears → distinct
"still initializing / could not be confirmed" error, exit != 0) via
`test_launch_read_timeout_then_poll_absent_errors`, and the unreachable-during-poll
path via `test_launch_read_timeout_then_server_unreachable_errors_distinctly`.

### 4. Manual F542 repro (dead tmux session name → reconciled once) — no server needed
Drives `reconcile_dead_session_terminals()` against a fake backend whose session
is absent, asserting the row is reconciled exactly once and a live-session row is
never touched:
```bash
uv run python - <<'PY'
from types import SimpleNamespace
from unittest.mock import MagicMock
from cli_agent_orchestrator.services import terminal_service as ts

# Fake tmux backend: cao-orch3 is DEAD, cao-live is ALIVE
backend = MagicMock()
backend.supports_event_inbox.return_value = False
backend.session_exists.side_effect = lambda name: name == "cao-live"

deleted, cleaned, settled = [], [], []
ts.get_backend = lambda: backend
ts.db_list_all_terminals = lambda: [
    {"id": "dead-sup", "tmux_session": "cao-orch3", "tmux_window": "dead-sup", "init_state": "ready"},
    {"id": "dead-w1",  "tmux_session": "cao-orch3", "tmux_window": "dead-w1",  "init_state": "ready"},
    {"id": "alive",    "tmux_session": "cao-live",  "tmux_window": "alive",    "init_state": "ready"},
]
ts.provider_manager = SimpleNamespace(cleanup_provider=lambda tid: cleaned.append(tid))
def _delete(tid, *, preserve_warm_intent):
    deleted.append(tid); return {"terminal_deleted": True, "intent_deleted": False}
ts.delete_terminal_and_warm_intent = _delete
def _settle(*, receiver_ids):
    settled.extend(receiver_ids); return SimpleNamespace(busy_aborted=False)
ts.settle_pending_orphan_messages = _settle

r1 = ts.reconcile_dead_session_terminals()
print("first:", r1, "deleted:", sorted(deleted))
assert r1 == {"reconciled": 2, "skipped_session_live": 1}
assert sorted(deleted) == ["dead-sup", "dead-w1"]        # dead session rows reconciled
assert "alive" not in deleted                            # live session untouched
assert backend.session_exists.call_count == 2            # ONE probe per distinct session (2 sessions)

# Idempotent: the rows are "deleted" — a second sweep over the survivors is a no-op.
ts.db_list_all_terminals = lambda: [
    {"id": "alive", "tmux_session": "cao-live", "tmux_window": "alive", "init_state": "ready"}]
r2 = ts.reconcile_dead_session_terminals()
print("second:", r2)
assert r2 == {"reconciled": 0, "skipped_session_live": 1}
print("F542 reconcile-once + idempotent + live-untouched OK")
PY
```

### 5. Optional box e2e (deferred, per 2026-08-28 directive)
On a grok box with a running cao-server: `tmux kill-server`, restart cao-server,
then confirm the journal is quiet (no repeating
`Failed to get history from <session>:<window>` at ERROR) and the dead rows are
gone (`cao status` shows them reconciled). This is the acceptance step neither
F541 nor F542 exercised on the laptop.


---

## r9 — merge origin/main (F557 + F556) into lane cao/e08be272

**Context.** origin/main advanced to `ecdefb94` carrying **F557** (`preflight_launch`
+ `ProfileNotFoundError` in `providers/claude_code.py`, and the `E-PROFILE-NOT-FOUND`
400 arm in `api/main.py`) and **F556** (inbox changes). Merged into the F548 gate lane
(`cao/e08be272`, tip `c7e50465`). Goal: BOTH F548 (settle-poll / auth-scan / trust-modal
handling + `ClaudeAuthError`/`E-CLAUDE-AUTH`) AND F557 (fail-loud profile preflight) survive
intact, with every test from both sides kept.

**Command.** `git -C /data/cao-scratch/wt-f548-gate merge origin/main`.

### Conflicted hunks (2 files, 1 hunk each — both add/add against base `f6c35046`)

1. **`src/cli_agent_orchestrator/providers/claude_code.py`** (lines ~78–92, add/add).
   - HEAD (F548) added `class ClaudeAuthError(ProviderError)` (`code = "E-CLAUDE-AUTH"`).
   - origin/main (F557) added `class ProfileNotFoundError(ValueError)` (`code = "E-PROFILE-NOT-FOUND"`,
     with `__init__(profile_name, store_dir)` + `.detail`).
   - Base had neither. **Resolution:** keep BOTH class definitions, ClaudeAuthError first
     then ProfileNotFoundError. They are independent, non-overlapping types.

2. **`src/cli_agent_orchestrator/api/main.py`** (line ~129, add/add import).
   - HEAD imported `ClaudeAuthError`; origin/main imported `ProfileNotFoundError` — both
     `from cli_agent_orchestrator.providers.claude_code`.
   - **Resolution:** single combined multi-name import bringing in BOTH `ClaudeAuthError`
     and `ProfileNotFoundError`.

`test/providers/test_claude_code_unit.py` auto-merged cleanly (no conflict) — kept both
sides' tests.

### Why the two designs do NOT conflict (verified, not assumed)

- The exception-handling ARMS auto-merged into distinct handlers with no ordering clash:
  - `except ClaudeAuthError` (F548) → 500 JSON, in the orchestration/send-path handler
    (`api/main.py` ~3761).
  - `except ProfileNotFoundError` (F557) → 400 JSON, in the terminal-create handler
    (`api/main.py` ~4288), correctly placed BEFORE the generic `ValueError` arm so the
    subclass is not masked as a 404.
- The two behaviours live in separate provider methods: F557 raises `ProfileNotFoundError`
  from `preflight_launch` (`claude_code.py` ~599, before any resource alloc); F548 raises
  `ClaudeAuthError` from `_handle_startup_prompts` (`claude_code.py` ~1237, after auth-marker
  scan). No shared code path.
- Name-collision check: `api/main.py` line ~3262 has a FUNCTION-LOCAL
  `from ...services.profile_store import ProfileNotFoundError` — a different, function-scoped
  symbol that does NOT shadow the top-level `claude_code.ProfileNotFoundError` used by the
  terminal-create arm. Both usages resolve correctly.

### Verification (targeted, `-n0`, no laptop full suite)

| test file | result |
|---|---|
| `test/providers/test_claude_code_unit.py` | 201 passed |
| `test/providers/test_f548_gate_repro.py` | 9 passed |
| `test/api/test_api_endpoints.py` | 118 passed |
| `test/services/test_terminal_service_coverage.py` | 14 passed |
| **total** | **342 passed, 0 failed** |

F548 repro tests and F557 `test_preflight_launch_*` / `test_missing_profile_fails_loud`
pass together in the same run — both sides confirmed live.

- **black / isort** on the two touched files: no changes (already clean).
- **mypy --strict net-new:** 0. Merged tree = 132 errors on the two files; origin/main
  baseline = 132 identical error signatures (pre-existing memory/relationship endpoint
  `no-untyped-def` / `type-arg` noise). No new error introduced by the resolution.
