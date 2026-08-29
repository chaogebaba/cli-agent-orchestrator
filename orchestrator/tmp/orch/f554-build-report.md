# F554 (#410) build report — sqlite `database is locked` in deferred init

**Branch:** `cao/c8f4e63d`  **Tip:** `cf96974da819d1117a50ee8cf93bceefdd58190a`
**Worker:** `c8f4e63d` (kiro_dev)  **Assigned by:** `60d393b2`

## Symptom (from issue #410)

A `cline_dev` worker (`f7528b7e`) died in deferred provider init:
`code=deferred_init_internal deadline_s=180.0 … reason=OperationalError('(sqlite3.OperationalError) database is locked')`.
Concurrent load: 2 `kiro_reviewer` spawns + 4 warm `kiro_dev` + secretary.

## Root cause (PROVEN, not restructured)

1. **The shared ORM engine set no connection pragmas.**
   `src/cli_agent_orchestrator/clients/database.py:1400`:
   ```python
   engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
   ```
   A repo-wide grep for `listens_for(engine` / `"connect"` / `journal_mode` /
   `busy_timeout` / `PRAGMA` found **no `@event.listens_for(engine, "connect")`
   handler** — nothing ever set `journal_mode` or `busy_timeout` on ORM
   connections. So the engine ran on SQLite's default `journal_mode=DELETE`
   (whole-database **exclusive** lock for the entire duration of any write) with
   no explicit `busy_timeout`. With no busy_timeout the C layer returns
   `SQLITE_BUSY` (**"database is locked"**) **instantly** to a would-be writer
   instead of waiting — so under concurrent assigns writers fail rather than
   serialise.

2. **The deferred-init ready-commit had no busy-retry.**
   `mark_terminal_init_ready` (database.py:~5074) does a `db.commit()` on that
   engine with **no** retry loop. Contrast `claim_deferred_init_failure`
   (database.py:~5188) which *does* have a `busy_attempts`/`busy_delay_s` loop and
   uses `BEGIN IMMEDIATE`. The reported death was on the **success** ready-write
   path (`deferred_init_internal`), the one path with neither pragma protection
   nor a Python-level retry — exactly where the lock surfaced.

3. **Disproof of alternatives.** The lock is not from a foreign process: the
   only other `sqlite3.connect` sites are (a) `workflow_journal.py:294` which
   *already* sets `PRAGMA busy_timeout` (so it is not the offender) and (b)
   migration/PRAGMA-introspection helpers in `database.py` (2091–2199) that run
   at `init_db`, not during steady-state assigns. The contention is same-engine
   writer-vs-writer, resolved by the engine-level fix.

## Fix (smallest correct; no DB-layer restructure)

`src/cli_agent_orchestrator/constants.py`
- New constant `CAO_DB_BUSY_TIMEOUT_MS = 5000` (>= 5s per the issue).

`src/cli_agent_orchestrator/clients/database.py`
- New `@event.listens_for(engine, "connect")` handler `_set_sqlite_pragmas`
  applied to **every** pooled connection, once each:
  - `PRAGMA journal_mode=WAL` — readers + one writer coexist; no whole-db
    exclusive lock (persistent per-database).
  - `PRAGMA busy_timeout=5000` — a would-be writer **waits** inside SQLite up to
    5s for the lock instead of raising immediately (per-connection).
  - `PRAGMA synchronous=NORMAL` — standard safe durability level paired with WAL.
  - Interpolates only the trusted module constant (pysqlite has no bound param
    for PRAGMA).
- `mark_terminal_init_ready` now wraps a single attempt
  (`_mark_terminal_init_ready_once`, the former body, unchanged) in a **bounded
  SQLITE_BUSY retry** (`busy_attempts=4`, `busy_delay_s=0.025`), mirroring
  `claim_deferred_init_failure`. Only pure busy/locked `OperationalError`s are
  retried (`_is_sqlite_busy_error`); every other outcome (veto, invariant
  breach, abandonment interrupt, success) is returned/raised on the first
  attempt exactly as before — public signature/name preserved, all existing
  callers/tests unaffected.

## Mutation note

Behavioural change to the shared SQLite connection: **journal mode flips from
DELETE to WAL** for the production DB the first time a process opens a
connection after this ships. WAL creates sidecar `-wal`/`-shm` files next to the
DB file and is a persistent per-database property. This is the intended fix and
is backward-compatible (SQLite auto-manages WAL checkpointing); no schema or data
migration is involved. `busy_timeout`/`synchronous` are per-connection and reset
each open.

## Tests (targeted only — no laptop suite)

New: `test/clients/test_f554_sqlite_concurrent_writes.py` (3 tests). Real
on-disk DB under `/data/cao-scratch/c8f4e63d` (never `/tmp`, per scratch policy):
- `test_production_engine_sets_wal_and_busy_timeout` — shipped `db.engine`
  opens connections in `journal_mode=wal` with `busy_timeout >= 5000`.
- `test_concurrent_writes_do_not_raise_database_locked` — two threads each
  writing 25 terminal rows through the real client concurrently raise **no**
  `OperationalError`; all 50 rows durable.
- `test_ready_commit_retries_on_transient_busy` — a simulated transient busy on
  the first commit is swallowed by the bounded retry; the ready write then
  commits and is durable.

Run (suite slot held by sibling terminal `940a5884`; used the sanctioned
scoped-file bypass `CI=1 -p no:suite_slot`, F497 precedent; explicit
`--basetemp` to sidestep a stale pytest-tmp cleanup hang unrelated to this
change):

```
CI=1 CAO_SKIP_RESOLVER_PROBE=1 uv run pytest \
  test/clients/test_f554_sqlite_concurrent_writes.py \
  -p no:suite_slot -p no:cacheprovider -n0 -q \
  --basetemp=/data/cao-scratch/c8f4e63d/pytest-bt --timeout=120
=> 3 passed in 0.53s
```

Regression guard (existing `mark_terminal_init_ready` behaviour):
```
uv run pytest test/services/test_f230_progress_fence_closed_db.py \
  test/services/test_ready_winner_race_probe.py \
  test/services/test_f110_deferred_init_watchdog.py ...
=> 16 passed in 12.88s
```

## Lint / types (touched files)

- `black` — clean (test file reformatted then clean).
- `isort` — clean (database.py constants import split to multi-line).
- `mypy --strict src/.../database.py src/.../constants.py` — **baseline 35
  errors, after 34 errors → zero net-new** (the module is not strict-clean at
  baseline; all pre-existing `no-untyped-def`/`no-any-return` in unrelated
  functions). My additions are fully annotated and flagged none.

## Diff --stat

```
 src/cli_agent_orchestrator/clients/database.py     |  89 ++++++++++-
 src/cli_agent_orchestrator/constants.py            |  10 ++
 test/clients/test_f554_sqlite_concurrent_writes.py | 168 +++++++++++++++++++++
 3 files changed, 266 insertions(+), 1 deletion(-)
```

## Box actions ledger

No offload-box work was performed. All commands ran on the laptop in the
provisioned worktree `.cao/worktrees/c8f4e63d`. Targeted tests only (3 new + 16
regression), no full suite. Scratch under `/data/cao-scratch/c8f4e63d`.


---

## r2 — gate r1b remediation (GATE-NO → addressed)

Gate r1b (`/data/cao-scratch/f554-gate-report-r1b.md`) ruled GATE-NO on one
BLOCKING (B1, test-only) + one SHOULD (S1). The product change (WAL +
busy_timeout connect-listener + bounded retry) was ruled proven correct and
load-bearing (mutation 3/3 both mutants, controls green); only the new test and
a docstring needed work. Both fixed here. N1 (WAL sidecar note) needs no code.

### B1 (BLOCKING, TEST-ONLY) — fixed

Root cause (from the gate): the new test read `busy_timeout` off the shared,
module-global, **pooled** production `db.engine` mid-suite. `busy_timeout` is a
**mutable per-connection** PRAGMA; under the merge-gate xdist invocation
(`-n 2 --dist loadgroup`) a checked-out pooled connection reported `1000`, so
`assert int(busy_timeout) >= 5000` failed net-new — even though the product code
is correct. It passed only in isolation (`-n0`).

Fix (test-only; product code untouched): replaced
`test_production_engine_sets_wal_and_busy_timeout` with
`test_production_connect_listener_sets_wal_and_busy_timeout`, which exercises the
**actual production listener function** `db._set_sqlite_pragmas` on a dedicated
`create_engine(..., poolclass=NullPool)` engine pointed at an isolated scratch DB
file. NullPool opens a brand-new physical connection per checkout and never
reuses one, so the only code that has touched the connection is the production
listener — no shared-pool state can contaminate the read. It now also asserts
the exact value (`== CAO_DB_BUSY_TIMEOUT_MS`). The probe engine uses an isolated
`/data/cao-scratch` file (not `DATABASE_URL`), so the test never touches or
WAL-converts the production DB file.

### S1 (SHOULD) — fixed

Added a "Durability window" paragraph to the `CAO_DB_BUSY_TIMEOUT_MS` comment in
`constants.py` stating that `synchronous=NORMAL`+WAL keeps a COMMIT durable
against an **application** crash, but the last committed transaction(s) whose WAL
frames are not yet checkpointed **can be lost on OS crash / power loss** (unlike
`synchronous=FULL`) — an accepted trade for CAO's coordination DB (state is
reconstructable from live sessions on restart; the win is far fewer fsyncs under
the concurrent-assign load that caused #410). No code-behaviour change.

### Verification (r2)

- Targeted file, isolation: `CI=1 uv run pytest test/clients/test_f554_sqlite_concurrent_writes.py -p no:suite_slot -n0` → **3 passed**.
- xdist reproduction of the B1 config over the slice containing the file:
  `CI=1 uv run pytest test/clients -n 2 --dist loadgroup -p no:suite_slot -q`
  → **381 passed, 1 skipped, 0 failures** (previously B1 failed here). The new
  test is now isolation-safe by construction (own NullPool engine + own scratch
  file), so mixing in `test/services` cannot re-trigger it — the shared-pool read
  that B1 depended on no longer exists.
  - Note: the full `test/services test/clients` slice is a ~7k-test heavy suite;
    per box-ops it is a box job, not a laptop run (a laptop run of the full slice
    exceeded the 15-min tool ceiling). No offload box was configured/reachable to
    this lane this round (`scripts/box-run.sh` not present in the worktree,
    `CAO_BOXES` unset), so the scoped-but-representative xdist proof above stands;
    the shared-pool defect is structurally removed regardless of sibling tests.
- Lint/types on touched files: `black`/`isort` clean; `mypy --strict constants.py`
  → Success (docstring-only change this round).

### r2 diff --stat (this round, vs r1 tip 7f9b7db5)

```
 orchestrator/tmp/orch/f554-build-report.md         | (this r2 section)
 src/cli_agent_orchestrator/constants.py            |  (durability-window comment)
 test/clients/test_f554_sqlite_concurrent_writes.py |  (B1 test rewrite)
```

### r2 box-actions ledger

No offload-box work this round (no box configured/reachable to this lane;
`scripts/box-run.sh` absent from the worktree, `CAO_BOXES` unset). All commands
ran on the laptop in the provisioned worktree `.cao/worktrees/c8f4e63d`; scratch
under `/data/cao-scratch/c8f4e63d`; no `/tmp`. Targeted + scoped-xdist tests
only, no full suite on the laptop.


---

## r3 — fix-round after gate r2 GATE-NO (B1 vacuous busy_timeout, S1 net-new strict errors)

**Worktree:** `/data/cao-scratch/wt-f554-r3` on lane branch `cao/c8f4e63d` (start tip `a2b11640`).
**Scope:** TEST-ONLY. No product-code change (`database.py`, `constants.py` both byte-identical to r2 tip). Only `test/clients/test_f554_sqlite_concurrent_writes.py` changed.

### B1 (BLOCKER) fix — make the mandated busy_timeout mutation catchable

Root cause (confirmed by gate r2): pysqlite's DEFAULT `busy_timeout` on a fresh
connection is `5000` == `CAO_DB_BUSY_TIMEOUT_MS`, so the r2 NullPool rewrite's
`assert int(busy_timeout) == 5000` (read off a fresh connection AFTER the listener
ran) was satisfied by the platform default whether or not the listener set it —
VACUOUS for busy_timeout.

Fix: `test_production_connect_listener_sets_wal_and_busy_timeout` no longer relies
on a `connect` listener firing. It now:
1. Opens a raw DBAPI connection (`probe_engine.raw_connection().driver_connection`,
   NullPool, isolated `/data/cao-scratch/c8f4e63d` scratch file).
2. Drives the PRE-state to values DISTINGUISHABLE from the production post-state:
   `PRAGMA busy_timeout=0`, `PRAGMA synchronous=FULL` (journal_mode left at the
   `delete` default). Asserts the pre-state (`busy_timeout==0`, `synchronous==2`,
   `journal_mode!=wal`) so the post-assertions cannot be satisfied by the pre-state.
3. Invokes the SHIPPED function directly: `db._set_sqlite_pragmas(dbapi_conn, None)`.
4. Asserts the listener RAISED each pragma: `journal_mode==wal`,
   `busy_timeout==CAO_DB_BUSY_TIMEOUT_MS` (and `>=5000`), `synchronous==1` (NORMAL).

Because busy_timeout starts at 0 (not the vacuous 5000 default), dropping the
`PRAGMA busy_timeout` line now leaves it at 0 and the assertion fails.

**Mutation proof (RAN):**
- Baseline `database.py` sha256 = `3013a8fb0b4bc65ad8d361630ea99a3daa20b664744aece419c7abbff77e56ae`.
- Dropped the `cursor.execute(f"PRAGMA busy_timeout={CAO_DB_BUSY_TIMEOUT_MS}")` line from `_set_sqlite_pragmas`:
  ```
  CI=1 uv run pytest test/clients/test_f554_sqlite_concurrent_writes.py::test_production_connect_listener_sets_wal_and_busy_timeout -p no:suite_slot -n0 -q
  => 1 failed
     assert 0 == 5000
      +  where 0 = int(0)     # test/clients/test_f554_sqlite_concurrent_writes.py:164
  ```
  MUTANT KILLED (r2: mutant SURVIVED / 3 passed).
- Restored byte-identical: `database.py` sha256 = `3013a8fb0b4bc65ad8d361630ea99a3daa20b664744aece419c7abbff77e56ae` (unchanged), `git status` shows only the test file modified.
- Post-restore: `3 passed`.

### S1 (SHOULD) fix — net-new mypy --strict = 0 on the touched file

The r2 rewrite's `int(...)` on `.scalar()` (`Any | None`) produced 2 net-new
`[arg-type]` errors. The r3 rewrite reads pragmas via a raw cursor
(`.fetchone()[0]`), so those are gone; the only net-new error the raw-connection
approach introduced was `[union-attr]` on `raw.driver_connection` (`Any | None`),
fixed with `assert dbapi_conn is not None` immediately after the assignment.

Baseline (r1 tip `7f9b7db5`, `git show 7f9b7db5:…` through the same checker):
**8 errors** = {`[import-untyped]`×2, `[no-untyped-def]`×6}, zero `[arg-type]`, zero `[union-attr]`.
HEAD (r3): **8 errors** = {`[import-untyped]`×2, `[no-untyped-def]`×6} — identical multiset.
**Net-new = 0.**

### Verification (RAN)

- Targeted isolation `-n0`:
  `CI=1 uv run pytest test/clients/test_f554_sqlite_concurrent_writes.py -p no:suite_slot -n0 -q` → **3 passed**.
- `mypy --strict test/clients/test_f554_sqlite_concurrent_writes.py` → 8 errors, net-new 0 vs baseline (above).
- `black --check` + `isort --check-only` on the touched file → clean (1 file unchanged).
- Scoped xdist B1 config:
  `CI=1 uv run pytest test/clients -n 2 --dist loadgroup -p no:suite_slot -q` → **381 passed, 1 skipped, 0 failures** (27.77s).
  - NOTE: a first attempt produced 302 collection ERRORS
    (`FileNotFoundError: .../pytest-tmp/popen-gwN`) — a PRE-EXISTING race in the
    `test.plugins.basetemp_offload` prune (KEEP=3) against xdist worker tmpdirs on a
    stale/accumulated `/data/cao-scratch/pytest-tmp`, NOT a test/product fault.
    Confirmed by re-running green two ways: (a) fresh isolated basetemp
    (`CAO_PYTEST_BASETEMP=/data/cao-scratch/f554-r3-ptmp CAO_PYTEST_BASETEMP_KEEP=200`)
    → 381 passed/1 skipped; (b) after `rm -rf /data/cao-scratch/pytest-tmp` with the
    default basetemp → 381 passed/1 skipped.

### r3 diff --stat

```
 test/clients/test_f554_sqlite_concurrent_writes.py | 88 +++++++++++++++++-----
 1 file changed, 70 insertions(+), 18 deletions(-)
```
(plus the force-added, gitignored build report)

### r3 containment / box-actions ledger

No offload-box work this round. All commands ran on the laptop in the provisioned
worktree `/data/cao-scratch/wt-f554-r3` (lane branch `cao/c8f4e63d`); scratch under
`/data/cao-scratch/c8f4e63d` and `/data/cao-scratch/f554-r3-ptmp`; no `/tmp`;
`/data` mounted. Targeted + one scoped-xdist `test/clients` slice only; no laptop
full suite. All transient product mutations (busy_timeout drop) were applied ONLY
to exercise the gate and restored byte-identical, verified by sha256. Never touched
`~/` or `~/.claude`.
