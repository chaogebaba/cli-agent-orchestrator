# F524 (#379) — Build Report

**Issue:** F524 / #379 (P0) — supervisor→worker `send_message` stuck `pending` for 68 min, then delivered stale.
**Branch:** `cao/00ef81f8` (worker terminal-id branch; fx121 commit hook enforces this name).
**Git-SHA-fork:** 4e38138dcca5dbd531853ef7a4f68fd8acd2fab8 (code head; this docs-only report commit rides on top).
**Base:** `main` @ 4e873cfd.
**Worktree:** `/home/chao/VScode_projects/cli-subagents/.cao/worktrees/00ef81f8/cli-agent-orchestrator`
**Merge status:** NOT merged — F244 gate follows.
**Scope:** items 1–4 as assigned; AC#4 (mid-turn steering) deliberately OUT (coordinates with #210/F355), confirmed by supervisor.

---

## 1. Root cause (file:line)

The FX191 **delivery-obligation ladder** is the only machinery in the inbox
subsystem that provides *time-bounded escalation* and *sender-facing surfacing*
of an undelivered message:

- `services/delivery_service.py` — `convergence_tick()` drives OPEN obligations,
  `_escalate()`, `_check_stranded()`, `_check_health_warnings()`,
  `_reresolve_escalated()`, all keyed off `delivery.escalate_after_s`
  (default 120s, `services/config_service.py:533`).

That ladder is created **only for supervisor-mailbox receivers**:

- `clients/database.py:584` `_f413_after_insert` — an obligation row is inserted
  only when `target.logical_receiver_id` is set **and** a `MailboxModel` with
  that id has `role == "supervisor"`.
- `clients/database.py:574` `_f413_row_qualifies` — the shared predicate requires
  `logical_receiver_id is not None`.

A supervisor→worker `send_message` (the incident's msg 1210) is a
**direct-terminal** row: `logical_receiver_id IS NULL` (it addresses a concrete
terminal, not a durable mailbox). Therefore:

1. **No obligation row is created** → no escalation ladder → **no sender-side
   liveness at all** for the message. It ages silently in `pending`.
2. The only ways a direct-terminal row gets delivered are all **IDLE-gated**:
   - `request_delivery()` at enqueue (`services/inbox_service.py`), deferred
     while the receiver is PROCESSING;
   - the status-event consumer `InboxService.run()` at
     `services/inbox_service.py:2073` / `:2098`, which calls `deliver_pending`
     only when a `terminal.*.status` event reports IDLE/COMPLETED;
   - the reconcile sweep `reconcile_orphaned_messages` →
     `list_pending_receiver_ids_older_than` (`services/inbox_service.py:3391`),
     which re-drives `deliver_pending` but still only *delivers* on
     IDLE/COMPLETED.

   Worker `3ff35106` was continuously PROCESSING from 00:54 to ~02:01, so every
   trigger deferred and the row starved for 67 minutes.
3. **No staleness/supersession check exists at delivery time.** The wire text is
   built verbatim at `services/inbox_service.py:2406`
   (`combined = "\n".join(m.message for m in batch)`), so when the worker finally
   went idle the 68-minute-old ruling was delivered as if fresh.

**Why 1210 starved while 1211 flowed:** 1211 was routed to a supervisor mailbox
(`mb_d176ebe0`), so it *did* get an obligation and rode the ladder. 1210 was a
direct-terminal row and got none. This is not a global stall — it is a
structural gap for the direct-terminal route.

---

## 2. DB forensics (read-only)

DB: `~/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db`

**inbox rows** (`logical_receiver_id`):

| id   | sender    | receiver  | logical_receiver_id | orchestration |
|------|-----------|-----------|---------------------|---------------|
| 1208 | f5019a45  | 3ff35106  | NULL                | send_message  |
| 1210 | f5019a45  | 3ff35106  | **NULL**            | send_message  |
| 1211 | 69fb8219  | f5019a45  | **mb_d176ebe0**     | send_message  |

**delivery_obligation rows** (for 1208/1210/1211):

- 1210 → **no obligation row** (direct-terminal, never accepted by `_f413_after_insert`).
- 1211 → obligation exists, state `ACKED` (rode the ladder).

**inbox_delivery_attempt** for receiver `3ff35106` (chronological):

| started_at              | settled_at              | outcome   | pre→settled status_gen |
|-------------------------|-------------------------|-----------|------------------------|
| 2026-08-27 00:54:02.305 | 2026-08-27 00:54:13.932 | confirmed | 2→2 (delivered 1208)   |
| 2026-08-27 **02:01:01.834** | 2026-08-27 02:01:13.431 | confirmed | 3→4 (delivered **1210 STALE**) |
| 2026-08-27 02:20:22 …   | …                       | confirmed | …                      |

→ **67-minute gap** (00:54:02 → 02:01:01) with no delivery attempt to the
receiver at all, confirming the IDLE-gate starvation. No trace events existed
for 1210 (tracing added later), consistent with the no-obligation finding.

---

## 3. Fix (per file)

Smallest correct change. The obligation ladder is **not** rebuilt for arbitrary
terminals (large blast radius, coupled to `resolve_supervisor_target` and
mailbox targets). Instead the direct-terminal route gets its own escalation rung
in the existing reconcile sweep, plus a delivery-time staleness guard.

### `src/cli_agent_orchestrator/clients/database.py`
- **`list_stalled_direct_pending_messages(min_age_seconds)`** — returns
  `InboxMessage` rows that are `PENDING`, direct-terminal (`logical_receiver_id
  IS NULL`), older than `min_age_seconds`, whose **receiver terminal still
  exists** (join on `terminals`), and whose **sender is a real terminal** (not a
  `cao-*` / `:`-namespaced service sender — prevents notice→notice loops and
  meaningless routes).
- **`message_has_trace_kind` / `messages_with_trace_kind`** — existence checks
  (single / batch) over `inbox_message_trace_event`. `messages_with_trace_kind`
  backs the delivery-time banner check (one query per batch).
- **`record_message_trace_event(...)`** — append one trace-event row.
- **S1 atomicity** — `InboxMessageTraceEventModel` gains a **partial unique
  index** `uq_inbox_trace_f524_stall_surfaced` on `(message_id)` scoped to
  `kind = 'f524.stall_surfaced'` (every other trace kind stays append-only /
  multi-row). **`claim_message_trace_once(...)`** does an insert-or-ignore under
  that index and returns `True` iff THIS call won the claim.
- **S1-migration (upgrade path)** — `Base.metadata.create_all` only creates the
  index on a FRESH DB; it never adds an index to an existing table. So
  **`_migrate_f524_stall_surface_unique_index()`** (registered in `init_db()`
  right after `_migrate_fx191_trace_extension`, following the same
  `engine.begin` / `sqlite_master` convention) runs on every existing-DB
  startup: it **dedupes** pre-existing `f524.stall_surfaced` rows (keeping the
  LOWEST rowid per `message_id`, so a stray duplicate cannot block the unique
  index) and then `CREATE UNIQUE INDEX IF NOT EXISTS`. Idempotent.

### `src/cli_agent_orchestrator/services/inbox_service.py`
- Constant **`F524_STALL_SURFACED_KIND = "f524.stall_surfaced"`**.
- **`surface_stalled_direct_deliveries()`** — the direct-row escalation rung,
  invoked from `reconcile_orphaned_messages` (the deployed heartbeat entry
  point). For each stalled direct message older than
  `delivery.escalate_after_s`: it computes age/receiver-status, then
  **atomically claims** the surface via `claim_message_trace_once` (stamp
  FIRST). Only the winning claim routes the **one-shot failure notice to the
  ORIGINAL SENDER** (`create_inbox_message(sender="message-trace:<receiver>",
  receiver=<orig sender_id>, body=...)`). A losing racer skips silently — no
  duplicate. Because the stamp is committed before the observable notice, a
  crash between commits loses at most one notice; it never duplicates one.
- **Staleness banner at the real `deliver_pending` composition path** (the
  `combined = "\n".join(...)` site, immediately before the wire flows to
  `terminal_service.prepare_input(terminal_id, combined, shape_type)`): if any
  message in the batch carries `f524.stall_surfaced`, a
  `[CAO STALE-DELIVERY WARNING] … AGED and possibly SUPERSEDED …` banner is
  prepended so a late delivery is not consumed as a fresh instruction. This is a
  production branch inside `deliver_pending`, not a test-side reconstruction.
- Wired `surface_stalled_direct_deliveries()` into `reconcile_orphaned_messages`
  (after the older-than sweep, before `recover_stale_deliveries`), exception-isolated.

### `test/services/test_f524_direct_delivery_stall.py` (new)
Nine tests, all through **deployed entry points** (no direct banner
reconstruction; the reconciler test never calls the sweep method directly).

---

## 4. Test results (touched files only, per instruction)

### Re-gate correction (empirical reviewer, 2 blocking + 1 serious)
- **B1 (was blocking):** the first-round tests called `surface_stalled_direct_deliveries`
  directly, so deleting its call from the deployed `reconcile_orphaned_messages`
  left the suite green. **Fixed:** `TestF524ReconcilerWiring` now drives
  `reconcile_orphaned_messages()` (the heartbeat entry point) end-to-end and
  never calls the sweep method directly.
- **B2 (was blocking):** the first-round Leg-2 test **reimplemented** the banner
  branch in the test body, so stripping the production prepend stayed green.
  **Corrected explicitly:** the Leg-2 tests now exercise the real
  `deliver_pending` composition path and assert on the delivered wire text
  captured at the `send_prepared_input` seam — the banner is produced by
  production code, not the test.
- **S1 (was serious):** first-round surfacing was non-atomic
  (check→send→stamp across sessions, no uniqueness). **Fixed:** partial unique
  index + `claim_message_trace_once` insert-or-ignore; stamp-first, send-only-if-won.

### Re-gate correction round 2 (S1-migration, serious)
- **S1-migration:** the round-1 unique index existed only on FRESH databases —
  `init_db()` did not create it on an existing `inbox_message_trace_event`
  table, so on a deployed-upgrade DB the index was absent and two
  `claim_message_trace_once('f524.stall_surfaced')` calls both returned `True`
  (duplicate rows). **Fixed:** `_migrate_f524_stall_surface_unique_index()` runs
  on the upgrade path (dedupe-then-`CREATE UNIQUE INDEX IF NOT EXISTS`),
  registered in `init_db()`.

### Test inventory — `test/services/test_f524_direct_delivery_stall.py` (11 passed)
- Leg 1 stall surfacing: aged direct message surfaces to sender; fresh message
  NOT surfaced; service-sender (`message-trace:`) excluded; supervisor-mailbox
  row NOT surfaced here (rides FX191).
- **B1** reconciler wiring: `reconcile_orphaned_messages()` end-to-end surfaces
  the stalled row.
- **B2 / Leg 2** real path: `deliver_pending` delivers the banner-prefixed wire
  when the row is stamped; delivers the raw wire when it is not.
- **S1** atomic one-shot: repeat sweeps emit exactly one notice; a claim taken
  by a crashed prior sweep is not re-sent (degradation, not duplication).
- **S1-migration** upgrade path (`TestF524UpgradePathMigration`): a DB with the
  trace table but WITHOUT the index (and a pre-seeded duplicate pair) → after
  `init_db()` the index exists, the duplicate is deduped to one row, an
  unrelated trace kind is untouched, and claim-once holds; `init_db()` is
  idempotent on a second run.

### Mutation verification (each new test KILLS its mutant)
| Mutant | Injected change | Result |
|--------|-----------------|--------|
| B1 | delete `self.surface_stalled_direct_deliveries()` in `reconcile_orphaned_messages` | `TestF524ReconcilerWiring` **FAILED** (0 notices ≠ 1) — killed |
| B2 | neuter the banner prepend (`if stale_ids and False`) in `deliver_pending` | `test_surfaced_message_delivered_with_banner` **FAILED** (wire had no banner) — killed; `without_banner` still passed |
| S1 | `claim_message_trace_once` returns `True` on `IntegrityError` | both `TestF524AtomicOneShot` tests **FAILED** (duplicate notice) — killed |
| S1-migration | remove `_migrate_f524_stall_surface_unique_index()` from `init_db()` | both `TestF524UpgradePathMigration` tests **FAILED** (index absent on upgraded DB) — killed |

All mutants reverted; the 11 tests pass clean afterward (no `MUTANT` markers remain).

### Regression (related suites, unchanged behavior)
- `test_f165_real_sqlite_reconciler.py` + `test_inbox_service.py` +
  `test_f413_orm_listeners.py` + `test_message_trace_inbox_matrix.py` +
  `test_fx158_pull_reconciler.py` — **99 passed** (includes the new partial index
  and the surfacing wiring in the reconcile path).

`black`-formatted. `ruff` not installed in this env. Full suite NOT run locally
(per instruction; offload-box available if the gate wants it).

---

## 5. Acceptance-criteria coverage

| AC | Requirement | Status |
|----|-------------|--------|
| #1 | Root-cause why 1210 starved while 1211 flowed; name the gate | ✅ §1/§2 — obligation-ladder gate excludes direct-terminal (`logical_receiver_id IS NULL`) rows. |
| #2 | Supervisor→worker msg undeliverable within `escalate_after_s` surfaces to the supervisor as a failure | ✅ `surface_stalled_direct_deliveries()` routes a failure notice to the original sender. |
| #3 | Regression: enqueue to a receiver that stays PROCESSING across the window; sender learns delivery did not happen | ✅ `test_stalled_direct_message_surfaces_to_sender` (receiver PROCESSING, sender receives notice). |
| #4 | *Ideally* mid-turn steering path | ⛔ OUT of scope by supervisor decision (coordinates with #210/F355). |

Bonus hardening (consequence of the root cause): the stale-late-delivery banner
(Leg 2) prevents the specific incident harm — a builder acting on a delivered-late
ruling as if it were fresh. Per the re-gate, this banner is verified through the
real `deliver_pending` composition path (B2), not a test reconstruction.

---

## 6. Re-gate change summary

Round 2 (additive over the round-1 fix; no behavior removed):
- Added partial unique index `uq_inbox_trace_f524_stall_surfaced` +
  `claim_message_trace_once` (atomic one-shot; S1).
- Surfacing loop now stamps-first via the atomic claim and sends the sender
  notice only on the winning claim.
- New tests: reconciler-wiring integration (B1), real-path banner assertions
  (B2), atomic one-shot / crash-safe (S1) — each mutation-verified.

Round 3 (S1-migration):
- Added `_migrate_f524_stall_surface_unique_index()` to `init_db()` so the
  unique index is created (with pre-dedupe) on existing/upgraded databases, not
  only fresh ones.
- New tests: `TestF524UpgradePathMigration` (index-creation + dedupe on an
  existing DB; idempotent) — mutation-verified by removing the migration call.
