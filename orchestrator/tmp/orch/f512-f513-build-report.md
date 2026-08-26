# F512 (#367) + F513 (#368) — build report

**Worker:** kiro_dev terminal 49124f29
**Branch:** `cao/49124f29` (fork repo `chaogebaba/cli-agent-orchestrator`)
**Base:** `b5425ca575eee3e4c4bcc3c50b9b100266058d97` (matches installed build / diagnosis checkout)
**HEAD after fix:** `73e2d923` (pushed to `origin/cao/49124f29`)
**Worktree:** `/home/chao/VScode_projects/cli-subagents/cli-agent-orchestrator/.cao/worktrees/49124f29`

## Status: CODE COMPLETE + TARGETED TESTS GREEN; FULL SUITE BLOCKED (boxes down)

Both fixes implemented, committed, pushed. Targeted regression tests written and
passing locally. **Full suite could not run: all four allowed offload boxes
(cursor-1/3/4/5) are unreachable / auto-suspended.** M8 forbids running the full
suite on the laptop while boxes exist, so I stopped rather than deviate. See
"Blocker" below — needs a supervisor decision.

---

## F512 (#367) — MCP delete_terminal 409 cause passthrough

**Root cause (confirmed against source):** `mcp_server/server.py` `delete_terminal`
had two 409 branches (direct-response path + `requests.HTTPError` path). Both
checked only `protection_indicators = ("ready_base","protected","cascade","subtree")`
and collapsed *every other* 409 detail — `resume_in_progress`,
`rebind_in_progress`, `cascade_quiesce_unstable`, `cascade_outside_caller_subtree`,
and genuine cleanup-deferrals — into the identical generic
`_cleanup_deferred_message` ("cleanup is pending; retry ... after the {provider}
process exits"). An operator could not tell lease contention from cleanup
deferral, and `force` semantics were opaque.

**Fix:**
- Added a single shared classifier `_classify_delete_409(detail, terminal_id)`
  plus `_DELETE_409_PASSTHROUGH_INDICATORS` (the four protection substrings +
  the four lifecycle/rebind/cascade codes). Both 409 branches now call it.
- Recognized causes → the real API `detail` is surfaced **verbatim**
  (`Failed to delete terminal: 409 Conflict (<detail>)`).
- Empty / unrecognized detail (i.e. an actual `cleanup_provider=False` deferral,
  whose API detail is `cleanup deferred for terminal '…'`) → still the
  provider-aware generic "cleanup is pending" message. The `success:false`
  payload path (200-with-body) is unchanged — it corresponds to the API's genuine
  cleanup-deferral response, so the generic message is correct there.
- **`force` wiring made explicit** (it was already correct end-to-end, per the
  diagnosis): the `force` Field description + docstring now state that it is
  forwarded verbatim as `?force=true` on `DELETE /terminals/{id}` and reaches the
  API cleanup-force path (`terminal_service.delete_terminal(force=True)` →
  bypass `cleanup_provider() is False`), and that it does **not** bypass the
  F513 lease gate. No behavioral change to forwarding — `params={"force": force
  is True, ...}` was and is present.

**Files:** `src/cli_agent_orchestrator/mcp_server/server.py`

## F513 (#368) — session-lifecycle lease starves unrelated deletes

**Root cause (confirmed):** `services/session_lifecycle_lease.py` exposes a
process-local shared/exclusive lock keyed **only by `session_name`**.
`_delete_terminal_inner` (`terminal_service.py`) called
`acquire_session_lifecycle_exclusive(session_name)` and raised
`RuntimeError("resume_in_progress")` **instantly** if it returned `None`. That
returns `None` whenever *any* terminal on the session holds a shared lease —
including an unrelated terminal Y's deferred-init background task, which holds
its shared lease for the **entire** `provider.initialize()` (released only in the
bg task's `finally`, up to the F509 ~180s watchdog). So a delete of X got a
spurious instant 409 for minutes because of Y's init.

**Fix chosen — bounded wait (smaller safe change, per issue's "pick the smaller
safe change"):**
- Added `acquire_session_lifecycle_exclusive_blocking(session_name, *, timeout_s,
  poll_interval_s=0.25)` to the lease module. It polls the existing non-blocking
  `acquire_session_lifecycle_exclusive` and sleeps between attempts **without
  holding the module `_guard`** (verified by test), so other sessions' lease ops
  proceed concurrently. Returns the token on success, `None` on timeout.
- `_delete_terminal_inner` now calls the blocking variant with
  `timeout_s = ConfigService.get("delete.lifecycle_lease_wait_s", 5.0)` before
  surfacing `resume_in_progress`. The exclusive-during-teardown invariant is
  preserved; only the *instant-fail* behavior changed to a bounded wait.

**Why bounded-wait, not per-terminal rescope:** the issue offered either option.
Per-terminal scoping would require threading `terminal_id` through the whole
create/delete/rebind mutual-exclusion machinery and changing the shared/exclusive
semantics (create takes a *session-wide* shared lease precisely to serialize
against session-wide mutations) — a large, cross-cutting, higher-risk change. The
contended holder here is normally a transient sibling init that releases within
seconds, so a short bounded wait converts almost all spurious 409s into
successful deletes while capping at a few seconds so a genuinely wedged init
(F509 watchdog) still eventually yields the 409 rather than blocking forever.
Default 5s is well under the F509 deadline and is config-overridable.

**Files:** `src/cli_agent_orchestrator/services/session_lifecycle_lease.py`,
`src/cli_agent_orchestrator/services/terminal_service.py`

---

## Tests

New file: `test/services/test_f512_f513_lease_and_passthrough.py` — **27 cases**, all pass.

F512:
- lease/rebind/cascade codes surfaced verbatim (parametrized over the 4 codes ×
  both 409 branches) — asserts the code is present and "cleanup is pending" is NOT.
- protection details (`ready_base`/`protected`/`cascade…`/`subtree…`) verbatim.
- genuine `cleanup deferred` detail → generic provider-aware message (both branches).
- empty detail → generic fallback (both branches).
- direct unit test of `_classify_delete_409`.
- `force=True` is forwarded as `params["force"] is True` on the DELETE.

F513:
- blocking acquire succeeds immediately when free.
- blocking acquire times out to `None` when a shared holder persists (asserts it
  actually waited ~the timeout, not instant-fail).
- **core:** blocking acquire succeeds after a sibling shared holder releases
  mid-wait (thread releases at 0.2s, acquire with 2s budget succeeds).
- blocking acquire does NOT hold `_guard` while waiting (a different session's
  lease op proceeds concurrently).
- integration-shape: `_delete_terminal_inner` raises `resume_in_progress` only
  after the bounded wait and passes the configured timeout into the blocking acquire.

### Local test results (targeted)
```
uv run pytest test/services/test_f512_f513_lease_and_passthrough.py \
             test/services/test_f493_delete_wedge.py \
             test/mcp_server/test_terminal_cleanup.py -q
=> 56 passed
```
(27 new + 29 pre-existing F493/cleanup regressions — no regressions.)

`uv run python -m py_compile` OK on all edited files. black (line-length 100):
my edited regions are clean; the only black-flagged lines in these files are
**pre-existing drift outside my hunks** (server.py 408/1244/1560/3156,
terminal_service.py 297/2018), which I deliberately left untouched (scope
discipline). isort: no changes.

### Full suite — NOT RUN (blocker)
Per M8 the full suite must run on an offload box, never the laptop while boxes
exist.

## Blocker (needs supervisor decision)

All four allowed boxes are unreachable/auto-suspended:
```
box-run: box@cursor-1 unreachable (auto-suspended?) — skipping
box-run: box@cursor-3 unreachable (auto-suspended?) — skipping
box-run: box@cursor-4 unreachable (auto-suspended?) — skipping
box-run: box@cursor-5 unreachable (auto-suspended?) — skipping
box-run: no box free within 90s   (exit 75)
```
Options as I see them:
1. Supervisor wakes/resumes a cursor box (or names another active box), then I
   re-run: `git fetch origin cao/49124f29 && git checkout 73e2d923` on the box,
   `uv run pytest -q -m "not live and not e2e"` (full suite).
2. Supervisor authorizes a one-off laptop full-suite run as an explicit M8
   exception (boxes genuinely unreachable) — I'd run it and report.
3. Ship on the targeted-test evidence above and defer the full suite to CI/next
   box availability.

I've stopped here per the stop-and-ask directive rather than pick one.

## Box-actions ledger
- `scripts/box-run.sh` invocations (all from root repo, allowed set pinned via
  `CAO_BOXES="box@cursor-1 box@cursor-3 box@cursor-4 box@cursor-5"`):
  - `f512-orient` — orientation `ls/remote/status` — timed out (no reachable box).
  - `f512-orient2` (`-w 30`) — orientation echo — exit 75, no box free.
  - `f512-orient3` (`-w 90`) — orientation echo — no box free.
  - No payload ever executed on any box (none reachable).
- Raw ssh: one read-only attempt `ssh box@cursor-1 '…ls/remote/status…'` — failed
  (connection timed out); nothing ran, no state changed.
- Checkout SHA left on boxes: N/A (never connected).
- Environment mutations on boxes: none.
- Temp files on boxes: none.
- Deviations: full suite not run because all allowed boxes are down — reported
  above as a blocker rather than run on the laptop.
