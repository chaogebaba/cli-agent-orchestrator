# F524 (#379) — Build Report

**Issue:** F524 / #379 (P0) — supervisor→worker `send_message` stuck `pending` for 68 min, then delivered stale.
**Branch:** `cao/00ef81f8` (worker terminal-id branch; fx121 commit hook enforces this name).
**Git-SHA-fork:** 6eb72983b595c9eef9c91fe0b6d76c08cc0817b2 (code head; this docs-only report commit rides on top).
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
- **`message_has_trace_kind(message_id, kind)`** — idempotency check (one notice
  per message).
- **`messages_with_trace_kind(message_ids, kind)`** — batch form for the
  delivery-time banner check (one query for a whole batch).
- **`record_message_trace_event(...)`** — append one `inbox_message_trace_event`
  row (own session, best-effort).

### `src/cli_agent_orchestrator/services/inbox_service.py`
- Constant **`F524_STALL_SURFACED_KIND = "f524.stall_surfaced"`**.
- **`surface_stalled_direct_deliveries()`** — the direct-row escalation rung.
  Invoked from `reconcile_orphaned_messages`. For each stalled direct message
  older than `delivery.escalate_after_s` not yet surfaced: routes a **one-shot
  failure notice to the ORIGINAL SENDER** (`create_inbox_message(sender=
  "message-trace:<receiver>", receiver=<orig sender_id>, body=...)`) so the
  supervisor learns the message did not land, and stamps `f524.stall_surfaced`
  (dedupes the notice AND arms the delivery-time banner). Best-effort per-row
  exception isolation.
- **Staleness banner at the delivery choke point** (the `combined = "\n".join(...)`
  site): if any message in the batch carries `f524.stall_surfaced`, a
  `[CAO STALE-DELIVERY WARNING] … AGED and possibly SUPERSEDED …` banner is
  prepended so a late delivery is not consumed as a fresh instruction.
- Wired `surface_stalled_direct_deliveries()` into `reconcile_orphaned_messages`
  (after the older-than sweep, before `recover_stale_deliveries`), exception-isolated.

### `test/services/test_f524_direct_delivery_stall.py` (new)
Both legs, driven through the real ORM path on the shared `real_sqlite_env` fixture.

---

## 4. Test results (touched files only, per instruction)

- `test/services/test_f524_direct_delivery_stall.py` — **6 passed**
  - Leg 1 stall surfacing: aged direct message surfaces to sender once; idempotent
    on repeat sweep; fresh message NOT surfaced; service-sender (`message-trace:`)
    excluded; supervisor-mailbox row NOT surfaced here (rides FX191).
  - Leg 2 stale-late-delivery: surfaced message gets the banner; un-surfaced
    message gets no banner.
- Regression (related suites, no changes):
  - `test_f165_real_sqlite_reconciler.py` + `test_inbox_service.py` — **44 passed**
  - `test_f413_orm_listeners.py` + `test_message_trace_inbox_matrix.py` +
    `test_fx158_pull_reconciler.py` — **55 passed**
- `black`-formatted. `ruff` not installed in this env. Full suite NOT run locally
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
ruling as if it were fresh.
