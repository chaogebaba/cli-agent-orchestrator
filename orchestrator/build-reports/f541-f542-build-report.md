# F541 (#397) + F542 (#398) — Build Report

## Provenance

| | |
|---|---|
| Base sha | `f6c35046` (`Merge 'cao/26643eb7' into main (F244 gated)`) |
| Branch | `cao/e08be272` (isolated worktree `e08be272`) |
| F541 commit | `a2f446a1` — `F541: launch confirm-then-attach on slow cold start (#397)` |
| F542 commit | `e2406a47` — `F542: reconcile dead-tmux-session terminal rows at startup (#398)` |
| Report commit | `F541/F542: build report` (this file) |

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
