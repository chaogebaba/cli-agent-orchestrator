# WPM4-A — Deferred-init hardening (P2)

**Status:** FROZEN r14 (2026-07-14). Dual FREEZE YES: codex r13 **FREEZE
YES** 0B/1S/0N zero-decision YES (universal helper coverage confirmed to
include both actual paired-delete seams; `preserve_warm_intent=False`
confirmed matching create-failure semantics; its S1 — raw-rollback
helper-failure visibility: `create_rollback_cleanup_failed` audit line,
mechanical rollback continues, original create error preserved — folded
here as the freeze-closing pin). grok r13 **FREEZE YES** 0/0/0
zero-decision YES (universal helper law composes with class-3 unwind +
r9.1 multi-step honesty; no design reason to retain a failed create's
intent; by-init-state owner split consistent with H5 predicate + class-1
fence). Gate history: 13 rounds dual-lane, gate artifacts
tmp/orch/gate-wpm4a-{codex,grok}-r{1..13}.md.
Freeze record — r13 (fold of gate r12: codex FREEZE NO B1/S1/N0 — B1 the
r12 sweep retirement was PREMATURE: `_rollback_terminal_creation`
(`terminal_service.py:237-251`) is a SECOND terminal+warm-intent deletion
seam — it deletes the warm intent in its own transaction, SWALLOWS every
exception, then independently deletes the terminal [probe: forced
`intent_store_busy` on the first delete → `(terminal,warm)=(0,1)`, and the
orphaned intent was CAS-REUSED by the next create] — folded: EVERY path
that semantically deletes both rows, explicitly including the raw creation
rollback, calls `delete_terminal_and_warm_intent(...,
preserve_warm_intent=False)` once (swallowed split call removed); only
then is the sweep's empty action set true. S1 the mid-helper-rollback
owner label was FALSE for ready rows (H5 selects pending/failure-terminal
only; a killed process writes no quarantine) — corrected: pending/
failure-terminal residuals → H5/settlement; READY residuals → class-1
verified-ready cleanup. grok r12: **FREEZE YES** 0B/0S/1N, zero-decision
YES — its N1 (create-rollback reverse two-step should route through the
helper for one-seam hygiene) is SUBSUMED by codex B1, now mandatory.
Prior: r12 (fold of gate r11: codex FREEZE NO B1/S1/N0 — B1 the
r11 dead-tmux-session warm-intent predicate was UNSOUND in BOTH directions
[probes: lawful `keep_bases` retention has `session_exists=False` after
`finalize_session` verifies the kill (`session_close_service.py:90-109,
128-142`); a last-window crash inside a still-live multi-window session is
MISSED; `session_exists` collapses backend errors to False (`clients/
tmux.py:519-525`, `herdr_backend.py:290-300`); and missing-terminal intents
are deliberately CAS-reused (`database.py:811-853`,
`test_epoch_recovery_service.py:58-85`)] — folded as STRUCTURAL elimination:
one atomic `delete_terminal_and_warm_intent` DB helper replaces the
two-commit seam, making warm-intent crash debris impossible; the sweep's
warm-intent arm (and with it the whole startup orphan-auxiliary sweep) is
RETIRED; S1 reconciler tasks moved to an INDEPENDENT terminal-service-owned
shutdown set (herdr resolver set unavailable on non-herdr backends) + the
row-gone cancelled `reconcile_delete_result` audit loss is DECLARED
(`reconcile_audit_lost_row_gone`), not silently dropped. grok r11: **FREEZE
YES** 0B/1S/1N — S1 last-observation pin (a lawful past-D completion IS the
loop's final observation; never a subsequent same-attempt saturation
admission) folded; N2 warm-base/`keep_bases` store naming folded into the
B1 rewrite. Older fold changelog (r1-r11, verbatim): blueprints/changelog-archive/wpm4a-deferred-init-hardening-r1-r11.md.
**Origin:** live silent-teardown incident 2026-07-14 (grok cold sign-in raced
single-shot artifact validation; failure was log-only; USER found the corpse in
tmux). SECOND live hit same day (aaa14f98, warm sign-in — race is a coin flip).

## Ground truth (codex-verified at merge `45cb0e9`; r2 anchors re-verified, no drift)

- `_persist_provider_runtime_identity` validates ONCE (`services/terminal_service.py:113-157`, call at :142).
- Grok validator: bare exists+nonempty (`providers/grok_cli.py:254-257`). Codex
  conflates zero-match and multi-match under `session_artifact_missing_or_ambiguous`
  (`providers/codex.py:545-550`); `session_artifact_identity_invalid` (`:551-554`)
  is terminal. Base contract: unclassified None/exception (`providers/base.py:100-101`).
- Except path (`terminal_service.py:1081-1102`): published UUID owners settle via
  `_settle_published_creation_failure` (`:1083-1087` → quarantine or
  confirmed-death delete, `:272-312`); legacy branch calls `delete_terminal`
  directly (`:1088-1091`). Whenever either path successfully DELETES the row
  (`:1834`), the notifier's re-read (`:929-934`) goes log-only (`:952-956`).
  Retained quarantine does NOT guarantee log-only.
- **Initial-send failure is INSIDE the deferred failure boundary** (empirical:
  `send_input` raise at `:1021-1028` is caught by the same broad branch at
  `:1070-1103`) — the failure law must cover it.
- `_notify_caller_of_deferred_failure` internal order correct (`:929-962`); enqueue best-effort.
- Deferred tasks + index process-memory only (`:80-86`); UNKNOWN is a
  status-monitor default (`status_monitor.py:901-929`), not a DB column
  (`clients/database.py:42-60`). Public delete does not cancel tasks
  (`:1655-1668`); HTTP DELETE runs the sync service delete in `asyncio.to_thread`
  (`api/main.py:2161-2170`); lifespan does not cancel/await deferred tasks
  (`api/main.py:552-595`). `create_inbox_message` rejects a missing RECEIVER
  (`clients/database.py:1363-1385`). Provider-session leases are process-local
  (`provider_session_lease.py`) and vanish on restart.

## Durable init lifecycle (spine of H2/H3/H5)

Schema change to `terminals`: `init_state` NOT NULL with a DB CHECK constraint
enforcing the vocabulary
(`init_pending` | `ready` | `init_failed_notified` | `init_failed_caller_gone`),
default/backfill `ready` (atomic migration); `init_started_at` (UTC, tz-aware);
`init_owner_epoch` (canonical UUID text, minted once per server lifespan
startup); `init_failure_token` (nullable UUID text, set exactly once by the H3
claim — the durable key mutation tests inspect; UNIQUE across retained rows,
immutable once set; row deletion releases the value — UUID collision
resistance is accepted, no token ledger [codex r4 N2]); `init_deadline_s` (nullable REAL — the EFFECTIVE validation
deadline used by this row's init; required non-null and finite in 1.0–600.0
whenever `init_state='init_pending'`; backfilled `ready` rows stay null).

Deferred creates publish `init_pending` + current epoch + non-null
`init_started_at` + the effective `init_deadline_s`, atomically. Sync creates
publish `ready` directly.

**Migration failure is FATAL** (codex r3 S3): the init-schema migration must not
follow the existing warn-and-continue pattern (`database.py:704-742`) — a
partial/old schema cannot support the H3/H5 predicates; abort server startup
visibly instead. It is ONE dedicated rollback-capable migration (single
transaction / table rebuild, NOT the existing per-ALTER separate commits —
codex r4 S4): CHECK vocabulary, cross-field pending invariant, UNIQUE token,
and `ready` backfill land atomically or not at all; migration tests assert all
pre-existing terminal columns/data survive the rebuild. One-time upgrade residual (codex r3 N2): backfilling all
pre-migration rows `ready` cannot identify a deferred init stranded by the
upgrade restart itself; pre-migration residuals remain an operator-audit
matter — H5's guarantee begins with rows published under the new schema.

**Ready ordering law (codex r2 B1):** the row stays `init_pending` through
initial task handoff. `init_pending → ready` commits ONLY after one of:
(1) identity validated+persisted and the create carries NO initial message;
(2) initial `send_input` returns successfully;
(3) the existing `TerminalInputBlockedError` branch durably queues the assigned
task (its live-worker notice behavior preserved unchanged).
Any other initial-send failure occurs while the row is still pending and is
claimed by H3. Crash bias at the non-transactional tmux-send/DB-ready boundary
is pinned to OBSERVABILITY: pending → failure notice on restart; never a
silently-ready worker whose assignment may not have been submitted. Accepted
dual (grok r3 S2): a crash after a successful tmux submit but before the ready
commit may yield a failure notice + destructive settlement of a pane that DID
receive its keys, and a re-assign may duplicate work — this residual is the
deliberate price of never-silently-ready. In arm (3), ready commits only after
durable queue SUCCESS; a queue exception routes to H3 while still pending. The
InputBlocked dialog notice itself stays `delete_worker=False`, non-settling, and
is NOT an H3 failure-token path (no second claim).

## Laws (H*)

- **H1 — typed retry loop with deadline.**
  (a) `providers/base.py` defines `RetryableArtifactValidation` /
  `TerminalArtifactValidation` exception types, each carrying an immutable
  stable `code: str` attribute; providers raise them. Codex SPLITS its combined
  code: zero matches → Retryable `session_artifact_missing`; multiple matches →
  Terminal `session_artifact_ambiguous`; identity-invalid stays Terminal. Grok
  missing/empty → Retryable. Any other exception (incl. `_persist`'s
  `RuntimeError`s) is terminal fail-closed. Tests assert classification by TYPE
  and notice construction by `code` — never `str(exc)` prose.
  (b) Loop at the deferred-init call site wraps the VALIDATE call only (never
  re-runs capture/identity-persist per poll). Sync create path and rebind
  validation stay single-shot — out of scope.
  (c) Immediate first observation after initialize; on Retryable only, per-poll
  abort check (terminal row exists AND tmux window alive AND task not cancelled —
  gone → terminal `worker_vanished`), then
  `await asyncio.sleep(min(POLL_INTERVAL, remaining))` with production constant
  `POLL_INTERVAL = 2.0` s (tests inject a clock/interval, no second config);
  exactly one final observation at the monotonic deadline. **Deadline origin
  (codex r10 B1):** ONE monotonic origin is captured at H1 validation-loop
  start, BEFORE the first admission attempt; deadline D = origin + stored
  `init_deadline_s`. (The earlier "measured from first observation" wording
  is superseded — it was circular: no origin exists before the first
  observation, and admission wait now precedes it.) **Slot-grant boundary
  (codex r10 B1):** an observation is TIMESTAMPED at dispatcher slot
  grant/dispatch-start, not validator entry. The final observation is
  lawful iff its slot grant is recorded no later than D — a grant at D−ε
  whose validator enters at D+ε is the lawful final observation and runs to
  COMPLETION past D (completion-after-D is intended); a grant recorded
  after D never calls the validator and returns
  `deferred_executor_saturated`. One rule, no post-acquire suppression
  check. **Last-observation pin (grok r11 S1):** ANY observation whose slot
  grant is ≤ D and which returns when wall time is already past D (or whose
  remaining budget is exhausted at the next grant decision) IS the loop's
  final observation — its result is processed as final (ready / Terminal
  code via H3 / still-Retryable → deadline-exhausted via H3); the loop
  NEVER schedules a subsequent admission for the same init attempt, which
  could only return `deferred_executor_saturated` on top of a lawful
  observation. The deadline DURATION is the same value stored as the row's
  `init_deadline_s` (captured once at deferred publish) — never a live settings
  re-fetch at loop start or per poll; the settings law is only the WRITER of
  that column (grok r4 S1). The H3 transactional claim is one named helper on
  the DB seam (e.g. `claim_deferred_init_failure`) — never a composition of
  stock `create_inbox_message` (own commit) with a separate status update.
  (d) Validation observations run off the event loop (thread executor). The
  same off-loop law covers the ONE-TIME capture/persist preparation (codex r5
  S1): fresh codex capture (`capture_codex_uuid`,
  `fork_context_service.py:180-217`) contains blocking `time.sleep(1)` waits
  and recursive filesystem walks — these never run on the event loop; the
  responsiveness drain covers capture, not just validation polls.
  (e) `asyncio.CancelledError` propagates — never classified as init failure.
  **Cancel/await split (codex r2 B3):** EXTERNAL deletion (public API/service
  delete) must (i) request cancellation thread-safely on the task's owning loop
  (`call_soon_threadsafe`), (ii) await task completion so its `finally` releases
  provider/session leases, (iii) only then run the CORE deletion seam; the
  quiesce bound is a fixed production constant `DEFERRED_TASK_QUIESCE_S = 10.0`
  (tests inject); on bound exceeded the delete returns a failure with stable
  code `deferred_task_quiesce_timeout` — it never claims success, and the
  row/task residual is left for retry/H5. **Scope (codex r9 B2):** this
  generic code + row-left promise applies ONLY when the in-flight work is
  ABANDONABLE or not yet started; a started MUTATING call overrides it with
  `quiesce_timeout_mutation_in_flight` + outcome-unknown (see the split
  below) — never both codes, never both row contracts, for one timeout. The `(task, owning_loop)` pair is
  stored at schedule time so the thread-safe cancel has its target.
  **Executor-future quiescence (codex r6 B2, mechanism corrected by codex r7
  B3):** awaiting the cancelled asyncio task is NOT quiescence — the task
  settles in microseconds while its blocking thread keeps running. And the
  r7 mechanism was itself broken: task cancellation PROPAGATES to a stored
  `run_in_executor` asyncio Future (probe: `future_cancelled=True` while
  `thread_finished=False`; join returns CancelledError, not completion).
  Corrected mechanism — one cancellation-shielded tracked-executor helper:
  – EVERY blocking call inside the deferred task routes through it — all
    eight current `to_thread` sites (`terminal_service.py:1016-1102`: two
    metadata reads, `send_input`, blocked-task queue, two notifications,
    settlement, delete) PLUS the newly off-loaded capture/persist (currently
    synchronous at `:1003-1005`) and each validation observation (codex r8
    N1 wording).
  – The helper submits to a DEDICATED service-owned DAEMON-THREAD dispatcher
    (codex r8 B2: `ThreadPoolExecutor.shutdown(wait=False,
    cancel_futures=True)` returns immediately but CPython still JOINS pool
    workers at interpreter exit — probe: process exit blocked until the
    running thread released; `cancel_futures` affects queued, not running,
    calls — so a pool cannot deliver the bounded-exit promise). The
    dispatcher runs each blocking call on a `threading.Thread(daemon=True)`,
    completing a per-call `concurrent.futures.Future` it creates
    (concurrency capped at `DEFERRED_EXECUTOR_MAX_WORKERS = 8`, fixed
    production constant, tests inject — grok r8 S4: drains must not assume
    default cpu-count). **Admission law (codex r9 B1):** the slot cap is
    enforced by CANCELLATION-AWARE ASYNC admission — the event loop is NEVER
    blocked on a `threading.Semaphore` (probe: inline acquire on a saturated
    semaphore stalled the loop 150ms; a 5ms ticker advanced zero times).
    Excess calls wait asynchronously in a bounded FIFO
    (`DEFERRED_ADMISSION_QUEUE_MAX = 32`, fixed production constant, tests
    inject — grok r10 N5; queue full → immediate
    `deferred_executor_saturated`) and remain cancellable while queued;
    slot-wait time is CHARGED against the call's remaining budget — for
    validation observations, against the H1(c) monotonic deadline. If no
    slot exists when the deadline arrives, the call terminates with stable
    local code `deferred_executor_saturated`; it NEVER pretends the final
    artifact observation occurred. **Queued-MUTATING typing (grok r10
    S1):** once a registry Future is typed MUTATING, quiesce timeout uses
    the mutation-in-flight contract even while the call is still
    admission-queued (never "not yet started" + row-left — the Future may
    later run); a queued MUTATING call that is CANCELLED before slot
    acquisition, however, provably never ran and settles as an ordinary
    cancelled entry. The
    registry retains that underlying Future (a running concurrent Future is
    NOT cancelled by asyncio cancellation); the task awaits it through an
    asyncio wrapper — cancellation hits the wrapper only. Daemon threads do
    not block interpreter exit. Exit-kill safety is NARROW (grok r9 B1):
    single-transaction atomicity holds ONLY for the H3 claim (one
    `BEGIN IMMEDIATE` txn — journal rollback to clean `init_pending`) and
    any call explicitly implemented as one DB transaction. Settlement and
    core delete are MULTI-STEP (rebind-lease loop / quarantine / stop
    FIFO+status / kill window / provider cleanup / then `db_delete_terminal`
    — `terminal_service.py:272-312`, `:1684-1834`): a daemon thread killed
    mid-settlement can leave PARTIAL teardown (window gone + DB row live, or
    DB row gone + FIFO running). Accepted residual, with a NAMED owner per
    cutpoint (codex r9 S1): window gone / row live → H5 (old-epoch pending)
    or quarantine/`rollback_kill_uncertain` settlement paths; quarantined
    half-state → the existing retained-quarantine settlement; the third
    residual class — terminal row GONE with a warm intent LIVE (probe:
    terminal_rows=0, warm_intents=1 — INVISIBLE to H5, which keys on
    terminal rows) — is ELIMINATED STRUCTURALLY rather than swept (codex
    r11 B1: NO observation available at startup can classify it. A
    generic absent-terminal predicate deletes lawful `keep_bases`
    retention [codex r10 B2 probe: swept 'retain-me']; the r11
    dead-tmux-session predicate is unsound in BOTH directions — lawful
    `keep_bases` retention has the session ABSENT, because
    `close_session(keep_bases=True)` runs `finalize_session`, verifies
    `session_exists is False`, and only then skips warm-intent deletion
    (`session_close_service.py:90-109,128-142`, locked by
    `test_session_close_service.py:119-128`); a last-window crash inside
    a still-live multi-window session is MISSED; and `session_exists`
    collapses backend errors to False (`clients/tmux.py:519-525` locked
    by `test_tmux_client.py:598-601`; `herdr_backend.py:290-300`), so it
    is never a destructive authority. Missing-terminal intents are
    moreover deliberately CAS-reused as replacement recovery by
    `create_terminal_with_warm_intent` (`database.py:811-853`, locked by
    `test_epoch_recovery_service.py:58-85`)):
    – EVERY path that semantically deletes both rows calls one named
      atomic DB helper `delete_terminal_and_warm_intent(terminal_id,
      preserve_warm_intent=...)` exactly once (codex r12 B1: the helper
      scoped only to the core-delete pair left the sweep retirement
      FALSE). Covered seams: the core-delete pair — terminal delete at
      `terminal_service.py:1834`, warm-intent delete at `:1837-1842` —
      AND the RAW creation rollback `_rollback_terminal_creation`
      (`terminal_service.py:237-251`), which today deletes the warm
      intent in its own transaction, SWALLOWS every exception, then
      independently deletes the terminal [probe: forced
      `intent_store_busy` on the first delete → `(0,1)` orphan, then
      CAS-reused by the next create — the exact residual the retired
      sweep owned]; the swallowed split call is REMOVED, the rollback
      uses `preserve_warm_intent=False` (a failed worker's intent is
      never retained). **Helper-failure visibility (codex r13 S1):** the
      rollback's outer best-effort boundary (`terminal_service.py:
      242-251` `except Exception: pass`) must not silently absorb a
      FAILED helper transaction — on helper failure the rollback emits
      one stable visible cleanup audit line
      `create_rollback_cleanup_failed`, CONTINUES the remaining
      mechanical rollback (pipe/FIFO/window), and preserves the ORIGINAL
      create error as the caller-visible error (helper failure never
      replaces or propagates over it; rows retained `(1,1)` →
      row-backed owners by init state). Helper semantics: preservation FALSE → terminal
      row and warm intent deleted in ONE transaction; preservation TRUE
      → terminal row only, leaving the intent as the lawful
      warm-base/`keep_bases` RETENTION state (grok r11 N2: this store is
      the warm-base retention store — never confuse it with the
      teardown-intent table's `issuing`/`issued_ok` provenance). A
      thread/process killed mid-helper (or a helper transaction that
      fails) rolls BOTH back — the terminal row survives, so the
      residual is row-backed. Row-backed residual OWNERS by init state
      (codex r12 S1 — H5 selects pending/failure-terminal rows only,
      and a killed process writes no quarantine): `init_pending` /
      failure-terminal rows → H5/settlement recovery; READY rows (the
      helper also serves public/session deletion of ready terminals) →
      class-1 verified-ready cleanup. The ambiguous orphan cannot
      exist.
    – NO startup sweep touches warm intents; a missing-terminal intent
      is always either lawful retention or CAS-reuse inventory, governed
      solely by the existing recovery authority above. No
      session-liveness observation participates in any warm-intent
      decision.
    – TRANSCRIPT BINDINGS: append-only epoch HISTORY by design
      (`database.py:107-121`, `:1085-1125`) — explicitly RETAINED, never
      swept; retention policy unchanged in this slice.
    – AUTO-RESPONDER state: process-memory only, NO durable rows
      (`auto_responder.py:195-205`) — excluded from any DB sweep; it
      cannot survive the crash in the first place.
    **Sweep retirement (supersedes grok r10 S3 taxonomy pin):** with the
    warm-intent arm structurally eliminated, transcripts retained, and
    auto-responder non-durable, the startup ORPHAN-AUXILIARY sweep has an
    EMPTY action set and is REMOVED from the design — no aux-only startup
    deletion surface exists in this slice. The r10 taxonomy ruling (aux
    recovery sits outside terminal-deletion classes 1-3) is retired WITH
    the sweep, not contradicted. Evidence must exercise
    process-exit cutpoints inside the REAL multi-step delete — after window
    kill, after quarantine, and INSIDE the atomic terminal+intent helper
    transaction — and show each residual's owner recovers it. No durable
    "delete_in_progress" marker in this slice.
  – Registry-entry LIFETIME: the entry (with generation) outlives task
    completion/cancellation and any done-callback — it is removed only when
    the underlying concurrent Future actually finishes, or is marked
    `quiesce_failed`.
  Quiescence = cancel task → await task → JOIN the retained CONCURRENT
  future (threading-level wait on the remaining budget), all within one
  shared `DEFERRED_TASK_QUIESCE_S`; this single contract is what class-2,
  class-3, external terminal delete, and graceful shutdown all invoke. On
  timeout: SKIP the invoker's own core deletion and fail visibly with the
  code the call TYPE dictates — `deferred_task_quiesce_timeout` (row left,
  H5/retry) when the in-flight call is ABANDONABLE/not-started;
  `quiesce_timeout_mutation_in_flight` (row outcome UNKNOWN) when it is
  MUTATING. One stable code and one row contract per call type, asserted
  by golden tests (codex r9 B2).
  **Abandonable vs MUTATING split (codex r8 B3):** a post-hoc guard cannot
  undo effects the orphaned Future already executed (probe: the settlement
  future deleted the row AFTER `quiesce_failed` was set). Tracked calls are
  typed:
  – ABANDONABLE (metadata reads, capture/persist, validation observations,
    `send_input`, blocked-queue write): on quiesce timeout the entry is
    marked `quiesce_failed` + generation; late completion NO-OPS — never
    proceeds to claim/settle/delete — when the entry is `quiesce_failed`,
    the row is no longer `init_pending`, or the generation mismatches; the
    row belongs to H5/retry (grok r7 S3, scope now LIMITED to this type).
  – MUTATING (H3 claim, settlement, delete, notice insert): NON-abandonable.
    Timeout with a mutating call in flight → outcome-UNKNOWN: report
    `quiesce_timeout_mutation_in_flight`, issue NO competing destructive
    action. **Live completion reconciler (codex r9 B3):** while the server
    is LIVE, completion of a timed-out MUTATING future schedules exactly
    ONE service-owned durable-state reconciliation — a late H3 terminal
    result drives settlement; a rolled-back still-pending result surfaces a
    visible retry outcome; a late settlement/delete records its actual
    result. H5-next-start is the FALLBACK only when shutdown/process loss
    prevents that callback; a live no-restart server never leaves a
    committed late H3 claim `init_failed_notified`-but-unsettled
    indefinitely (probe: settled=False, task_cancelled=True, no restart).
    **Reconciler result vocabulary + shutdown ownership (codex r10 S1):**
    the reconciliation outcome is one of a CLOSED audit-code set —
    `reconcile_h3_committed` (settlement driven), `reconcile_h3_rolled_back`
    (row still pending, visible retry outcome), `reconcile_settlement_result`,
    `reconcile_delete_result` — emitted as one audit log line each (golden
    asserts exact codes); reconciler tasks live in an INDEPENDENT
    TERMINAL-SERVICE-owned task set registered with lifespan shutdown
    (codex r11 S1 — the intent-resolver set is owned by the HERDR service
    and does not exist on non-herdr backends; the reconciler is a
    terminal-service facility on every backend): cancelled+awaited at
    lifespan shutdown. Shutdown-fallback scope (codex r11 S1): a
    reconciliation lost to shutdown between Future completion and callback
    completion falls back to H5/startup recovery for late H3
    committed/rolled-back and retained-settlement rows; a cancelled
    `reconcile_delete_result` callback whose Future already DELETED the
    row is NOT reconstructible by H5 (no row remains) — that audit loss is
    DECLARED, not silent: shutdown emits one visible
    `reconcile_audit_lost_row_gone` log line per such cancelled callback
    (durable state is already correct — the delete happened; only the
    audit record is lost). No await-completed-callbacks machinery is
    added.
    **Exclusive live ownership (grok r10 S2):** after any
    mutation-in-flight timeout on a terminal, live multi-step
    settlement/core-delete is owned ONLY by (i) the still-running MUTATING
    Future and/or (ii) the single service-owned reconciler callback for
    that Future; every other live surface no-ops or joins that ownership —
    none starts a parallel multi-step settle/delete. (Sequential retry
    after an OBSERVED failure remains lawful under H3's
    one-terminal-RESULT; concurrent parallel settlement is not.)
    A late-completing mutating future's effects are ACCEPTED as truth —
    an H3 claim's transaction is atomic and fenced by durable CAS/token
    authority (double-claims impossible); settlement/delete late effects
    are accepted as the one-terminal-result even though those calls are
    multi-step, NOT atomic (r9.1). Never promise both "row left" and
    "late mutation no-op."
  **Graceful-shutdown timeout (codex r7 B3, mechanism corrected r9):**
  CPython loop shutdown waits for DEFAULT-executor threads regardless of
  cancellation — hence the dedicated daemon-thread dispatcher, whose threads
  do not block interpreter exit. Lifespan shutdown: cancel+await tasks →
  join retained futures under the budget → on timeout mark entries per the
  abandonable/mutating split above and proceed with registry teardown
  (daemon threads are abandoned truthfully; durable `init_pending` rows
  remain for H5 next start; an in-flight MUTATING call is logged
  outcome-unknown, reconciled next start). Shutdown never claims quiescence
  it did not observe — the timeout path logs per unfinished entry. The INTERNAL deferred-failure settlement path calls the core
  deletion seam with cancellation disabled — it must never cancel/wait on the
  very task it runs inside (the legacy notifier's `delete_worker=True` therefore
  routes to the core seam, not the external wrapper). Graceful shutdown
  cancels+awaits deferred tasks before registry teardown (durable `init_pending`
  row remains for H5).
  (f) No out-of-band process-death detector: per-poll abort check + deadline,
  pinned as sufficient.
- **H2 — caller context captured at scheduling.** `caller_id` (+ profile,
  provider) snapshotted when the deferred task is SCHEDULED, from the published
  row, passed explicitly; the failure path never re-reads the worker row for
  routing. Guarantee scope: a notice is guaranteed iff the snapshotted receiver
  exists when the H3 transaction commits. Vanished caller → durable terminal
  transition (H3 caller-gone arm) + stable audit line `caller_gone_zero_notice`
  — lawful zero, no retry storm, no rollback-to-pending.
- **H3 — atomic claim with two terminal arms; at-most-once durable notice;
  full rollback on insert failure (r3 both-lane B1).**
  One `BEGIN IMMEDIATE` transaction; acquire the write lock FIRST, then decide
  receiver existence, then CAS on `init_pending`, insert, token, commit (this
  ordering makes "receiver exists at commit" meaningful against a concurrent
  caller delete — codex r3 S4). Exactly two terminal arms:
  – receiver exists → `init_pending → init_failed_notified` AND insert the
    ordinary PENDING notice AND set `init_failure_token` (same transaction);
  – receiver missing (explicit observation under this same transaction — the
    ONLY path to this state) → `init_pending → init_failed_caller_gone`, NO
    insert, token set, commit (audit line).
  ANY inbox insert/flush/commit failure with the receiver present rolls back
  the ENTIRE claim (CAS + token + insert), performs NO settlement, leaves the
  row `init_pending`, and emits a stable audit/error result — a later startup
  (H5) retries the atomic claim. DB failure is never evidence the caller
  vanished; terminalizing it as lawful zero is the silent-loss class this law
  exists to prevent (and is not ORM-commit-safe after a failed flush anyway).
  Busy/locked handling may use the repository's bounded immediate-write retry
  policy (`database.py:2067-2090` pattern); retry exhaustion also remains
  pending and visible.
  Both terminal states are excluded from all future notification claims and
  eligible for exactly ONE settlement decision — meaning one terminal RESULT,
  not one attempted call (codex r3 S5): if post-claim settlement raises before
  delete/quarantine completes, the terminal claim is retained and the startup
  sweep retries settlement with a visible audit outcome. Settlement runs AFTER
  the commit in every branch. Crash after commit/before settlement → startup
  sweep settles WITHOUT enqueueing again. Crash before commit → sweep claims
  and notifies once. Notice content law: stable local fields only, frozen
  template skeleton `code=… deadline_s=… token=… worker=… profile=… provider=…`
  (golden tests match this order); `deadline_s` is the row's stored
  `init_deadline_s`. Stable-code map (closed, codex r4 S2): typed validation
  exceptions use their `code` attribute; each distinct `RuntimeError` raise
  site in `_persist_provider_runtime_identity` (`:113-157`) maps to a stable
  code equal to its existing literal message token (build enumerates ALL raise
  sites; the notice golden locks the complete map — no builder-chosen names);
  `deferred_executor_saturated` is a member of this closed map — a terminal
  init-failure code carried in the H3 notice like any other, never a silent
  drop or a Retryable spin (grok r10 S1); any other unexpected exception
  maps to `deferred_init_internal`.
  NEVER `{e!r}` / provider text (current `:1093-1098` pattern banned here).
  Serialization (codex r4 N1): `deadline_s` renders via Python `repr(float)`;
  worker/profile/provider fields are locally generated identifiers and are
  REJECTED at notice construction if they contain whitespace/control
  characters, keeping the one-line golden stable. Rejection terminal behavior
  (codex r5 N1): the claim transaction ABORTS (full rollback), the row remains
  `init_pending` for visible repair, and a `deferred_init_internal` audit line
  is emitted at most once per claim attempt (per-sweep, not a hot loop);
  startup does not fail on it.
- **H4 — zero frozen-law contact + settlement truth preserved.** The notice is
  an ordinary PENDING inbox row riding normal delivery (WPM1/WPM2/WPM3
  unchanged; no new delivery statuses, no pane push, no watchdog exemption;
  failure notices open no stalled-callback episodes). Published UUID owners
  ALWAYS settle via `_settle_published_creation_failure` after the H3 commit —
  notification never substitutes bare deletion, never alters
  quarantine/`rollback_kill_uncertain` outcomes. Valid only with the H1(e)
  core-delete split (internal settlement bypasses the external cancel wrapper).
- **H5 — startup sweep on durable state only.**
  Predicate: `init_state='init_pending' AND init_owner_epoch != current` —
  claim+notify via H3, then truthful settlement. The H5 notice uses the frozen
  process-loss code `server_restart_during_deferred_init` (no original
  exception exists after process loss) and reports `deadline_s` from the row's
  stored `init_deadline_s` — NEVER the current process's settings, which are
  not historical truth (codex r3 B2). A pending row with a missing/invalid
  stored deadline is a corrupt row under the fail-closed rule below. `init_failed_notified` /
  `init_failed_caller_gone` rows → settle only, no second notice; if such a row
  retains `recovery_state=rollback_kill_uncertain`, it is SETTLEMENT-TERMINAL
  for the sweep (never re-probed destructively on later startups). Sweep
  settlement acquires leases afresh (process-local leases died with the old
  process; no-live-token form of published settlement; bare `delete_terminal`
  forbidden for published rows). Placement: after DB migration and plugin
  registry load, BEFORE accepting new creates. Pinned assumption: single active
  server process per DB (otherwise each sees the other's epoch as old).
  Corrupt/null-field pending rows fail CLOSED and visibly (log + no destructive
  action). Status-monitor UNKNOWN participates in NO predicate. Healthy
  post-restart fleet receives ZERO H5 notices (drain assertion).
  **Deletion-authority law (codex r4 B1 generalized by codex r5 B1).** Every
  terminal deletion surface is classified into exactly one of three semantic
  classes; raw `delete_terminal` / `delete_terminals_by_session` are NOT
  callable by generic service cleanup on a non-ready row outside these
  classes (an explicit H3/core authority seam is the only route):
  1. **Maintenance cleanup** — pre-registry `purge_stale_terminal_records`
     (`api/main.py:498` → `terminal_service.py:160-181`), retention
     `cleanup_old_data` (`cleanup_service.py:25-45`), and herdr startup
     cleanup of old rows (`herdr_inbox_service.py:146-169,212-230`): delete
     only VERIFIED `ready` rows; absent/corrupt/failure init states skip
     fail-closed and visibly. Enumerations must read init fields explicitly
     (`list_all_terminals` `database.py:1269-1283` and
     `list_terminals_by_session` `database.py:889-914` omit them today) — an
     absent init field never defaults to ready.
  2. **Spontaneous death of a pending worker** — herdr periodic reconcile
     (`herdr_inbox_service.py:285-295,331-368,443-454`) and `pane.closed`
     (`:660-714`) observing a missing tab/pane for an `init_pending` row.
     Fixed recipe (grok r6 B1 — the deferred task may still be LIVE in-process
     holding UUID/lifecycle leases in its `finally`,
     `terminal_service.py:1105-1116`; settling under a held lease can raise
     `resume_in_progress` or mis-quarantine):
     (1) if a deferred task is present → thread-safe cancel+await under the
     same `DEFERRED_TASK_QUIESCE_S` / `deferred_task_quiesce_timeout`
     contract; on timeout the class-2 action FAILS visibly and leaves the row
     for retry/H5 — never raw-delete;
     (2) H3 claim with code `worker_vanished` (CAS; row already terminal →
     settle-only, no second notice);
     (3) truthful settlement via the core seam.
     Cancel-first ordering is pinned (the task's `CancelledError` is non-H3
     per H1(e), so no competing claim). Invoker checklist (grok r9 S3): after
     `quiesce_timeout_mutation_in_flight`, the class-2/class-3 invoker NEVER
     calls settle/core-delete "to finish the recipe" — durable reconcile or
     H5 only. Dual-detector rule (grok r6 S3): the
     H1 per-poll abort check and herdr may both observe the vanish — the CAS
     keeps notices at one; the CAS-missing observer emits no notice, may
     still cancel a live task, and never raw-deletes. Failure-terminal rows
     remain owned by settlement recovery.
  3. **Intentional teardown** — public session delete
     (`session_service.py:212-240`), lifecycle close
     (`session_close_service.py:56-88`), flow recycle
     (`flow_service.py:237-263`), and herdr `workspace.closed` (`:731-750`)
     ONLY when provenance is proven (below):
     cancel+await ALL deferred tasks first (same `DEFERRED_TASK_QUIESCE_S` /
     `deferred_task_quiesce_timeout` contract as public terminal delete),
     then core deletion; NO failure notice — intent is not failure.
     Membership pins (grok r6 S2): public/MCP/API
     `terminal_service.delete_terminal` IS the class-3 external wrapper;
     `agent_step` / `script_runner` orphan cleanup must call it, never raw
     DB deletes; create-failure rollback paths that may have scheduled a
     deferred task are class-3 unwind — cancel if a task is present, then
     core (`terminal_service.py:249` is the RAW rollback delete, `:872` the
     under-lease rollback — both covered, distinctly named [codex r6 N1];
     the raw rollback's terminal+intent deletion is executed via the
     atomic `delete_terminal_and_warm_intent(...,
     preserve_warm_intent=False)` helper, never the swallowed split call
     [codex r12 B1]).
     **Teardown-intent provenance (codex r6 B1):** the `workspace.closed`
     wire event carries only `type`+`workspace_id` — no proof CAO asked for
     the close (`herdr_backend.kill_session` `herdr_backend.py:321-333`
     records nothing; a workspace can close outside CAO or before reconcile
     cached it). Class-3 zero-notice treatment of a close event is lawful
     ONLY against a durable teardown-intent record with a three-state
     lifecycle and ONE-ACTIVE-GENERATION cardinality (codex r7 B1):
     – At most ONE active intent per workspace/logical teardown (DB UNIQUE on
       workspace_id among non-final intents). Production `finalize_session`
       retries `kill_session` up to five times (`session_service.py:47-57`):
       a retry REUSES/SUPERSEDES the existing active intent (generation
       bump, same row) — retries never accumulate surplus authority; one
       event consumes THE intent, and a later spontaneous close finds none.
     – Lifecycle: CAO CAS-creates the intent in state `issuing` BEFORE
       sending `workspace close`; command returns success → CAS
       `issuing → issued_ok`; command failure (non-zero, exception,
       unresolved workspace — `kill_session` can return False,
       `herdr_backend.py:328-333`) → the SAME issuing path immediately
       CAS-voids it (grok r7 B1 — TTL alone lets an in-window spontaneous
       death consume a failed-close intent as proven class-3).
     – Event-before-ack (codex r7 B1, probe-proven): the herdr event loop is
       concurrent, so a genuine close event can arrive while the intent is
       still `issuing`. The ack-wait MUST NOT stall the herdr socket/readline
       loop (grok r8 B1 — dispatch is inline from the readline task,
       `herdr_inbox_service.py:540-565`; an inline 5s wait queues
       `pane.agent_status_*` and delivery wakes behind it): on finding an
       `issuing` intent the handler schedules a BACKGROUND generation-stamped
       resolver task (at most ONE outstanding per workspace_id) and returns
       to readline immediately. The resolver re-reads up to
       `INTENT_ACK_WAIT_S = 5.0` (fixed production constant): flips to
       `issued_ok` → consume, proven class-3; voided or still `issuing` at
       the bound → route UNPROVEN (observability bias — false notice over
       silent zero). Resolver tasks are OWNED by the herdr service (codex r8
       S1): tracked in a service set, cancelled+awaited when the service
       stops (before registry teardown); an unresolved durable intent is
       simply left for TTL/recovery — shutdown mid-ack-wait loses nothing.
     – Consumption: exactly once via CAS on the CURRENT ACTIVE row —
       `WHERE workspace_id=? AND state='issued_ok' AND unconsumed` (grok r8
       S3: never a stale generation snapshot, so a supersede landing during
       the wait cannot double-route); requires unexpired; unconsumed intents
       expire after `TEARDOWN_INTENT_TTL_S = 60.0` (belt-and-suspenders for
       a crash after a successful command). Intent-before-command ordering
       makes the event-arrives-before-ANY-intent race impossible for genuine
       closes. Accepted dual (grok r7 S4): a
     genuine close whose event arrives after TTL routes unproven → pending
     workers get a class-2 false-failure notice — observability over silent
     zero-notice. If the intent store is unavailable, ALL closes are
     unproven (fail-closed toward more notices, grok r7 N5). An UNPROVEN
     close (no matching live intent) is NOT class-3: pending rows route
     class-2 (`worker_vanished` recipe), ready rows class-1 cleanup,
     failure-terminal rows stay settlement-owned, corrupt/null `init_state`
     rows take the class-1 fail-closed visible skip (grok r7 S2 — never bulk
     raw delete).
     **Durable workspace map (codex r7 B2):** the wire event carries only
     `workspace_id`; terminal rows have `tmux_session` but no workspace id
     (`database.py:42-60`), `_workspace_to_session` is process-memory filled
     by reconcile (`herdr_inbox_service.py:316-329`), and the live-list
     fallback returns None for an already-closed workspace (`:626-658`) — so
     an uncached unproven close cannot identify its session. The ONE frozen
     authoritative mapping is a durable `workspace_id → session name`
     association written when the workspace becomes known: at workspace
     creation in the herdr backend (it holds both ids) and backfilled by
     reconcile for observed live workspaces. Map LIFECYCLE (grok r8 B2 —
     session names are REUSED after teardown, `herdr_backend.py:122-123,
     255-270`; a stale row would route a late/replayed close for old
     workspace W1 against the RECREATED session's live terminals): PK =
     workspace_id; a successful create for session `S` with new workspace
     W_new RETIRES every other map row for `S`; completing any close route
     for workspace W (proven class-3 OR unproven class-1/2/settlement)
     retires row W. **The current-workspace guard is UNIVERSAL (codex r8
     B1):** before ANY destructive route — proven OR unproven — the event's
     workspace_id must equal the CURRENT active durable mapping for that
     session/backend generation. A delayed close event for a retired
     workspace whose unexpired `issued_ok` intent still names session `S`
     CONSUMES/RETIRES the intent but performs NO terminal action (probe
     showed the unguarded proven path deleting the recreated session's
     pending workers with zero notice). Unproven event routing resolves the
     session via this durable map ONLY (never the live list); if the
     resolved session has no live terminals or session/backend records
     disagree → fail-closed visible no-op. No mapping → nothing routable: log visibly, fail-closed,
     no deletion (the rows, if any, remain for H5/reconcile). Map store
     unavailable/missing → treated like the intent store being down:
     fail-closed toward unproven/no-op, never a guess. The existing test mocking the closed
     workspace as still-live (`test_herdr_inbox_service.py:1225-1264`) is
     replaced by the uncached-absent drain below. Existing
     unconditional bulk-delete workspace-close tests
     (`test_herdr_inbox_service.py:887-923,1225-1264`) are split
     proven/unproven accordingly.
  AT STARTUP, H5 exclusively owns pending and failure-terminal init rows —
  exclusivity scoped to the startup sequence only; during live operation the
  in-process H3 path remains the claimer (grok r5 S1). H5
  claims/notifies/settles them (truthful settlement, never bare
  `db_delete_terminal`) before any generic ready-row cleanup touches them. A
  gone-window old-epoch pending row is precisely H5's incident class, not
  purge fodder. Note the herdr startup cleanup runs as a background task just
  before lifespan yields (`api/main.py:535-550`) and can overlap NEW creates —
  the class-1 ready-only fence, not sweep ordering, is what protects them.
  Standing rule (grok r6 N4): ANY future raw DB delete of terminal rows is a
  freeze violation unless the call site is classified 1/2/3 or is the
  H3/H5 post-claim authority seam.
  **Caller-gone audit recovery (codex r4 S1):** H5 re-emits the
  `caller_gone_zero_notice` audit line for a pre-existing
  `init_failed_caller_gone` row before settling it — a duplicate log line is
  safer than zero durable observability after a crash between commit and log.
  **Busy exhaustion at sweep (codex r4 S3):** under the single-active-server
  assumption, `BEGIN IMMEDIATE` contention during the pre-create sweep implies
  a violated assumption — bounded-retry exhaustion FAILS STARTUP visibly; no
  silent stranded-row acceptance.

## Design (surfaces)

| Surface | Change |
|---|---|
| `services/terminal_service.py` | H1 loop + abort checks; ready-ordering law; H2 snapshot at schedule; H3 two-arm atomic claim; H4 settle routing; H5 sweep; external cancel+await wrapper vs internal core-delete seam; lifespan cancel+await |
| `providers/base.py` | typed validation exceptions with stable `code` |
| `providers/codex.py` | split missing (retryable) vs ambiguous (terminal) |
| `providers/grok_cli.py` | raise Retryable for missing/inert |
| DB (`clients/database.py`) | 5 new `terminals` columns (init_state NOT NULL + CHECK vocabulary, init_started_at UTC, init_owner_epoch UUID text, init_failure_token UNIQUE-across-retained-rows, init_deadline_s REAL non-null-when-pending); atomic backfill `ready`; migration failure FATAL |
| Settings (`services/settings_service.py`) | `CAO_ARTIFACT_VALIDATE_DEADLINE_S`: registered float (defaults + env overlay + typed registry; NOT the int-only overlay); finite 1.0–600.0, default 60.0; blank/malformed/non-finite/out-of-range → WARN + 60.0 |
| `services/herdr_inbox_service.py` | class-1 ready-only startup/old-row cleanup; class-2 `worker_vanished` H3 route for reconcile/pane.closed on pending rows; workspace.closed: consume intent → class-3 cancel+await, else unproven routing (pending→class-2, ready→class-1, terminal→settlement) |
| `backends/herdr_backend.py` | `kill_session` commits the durable teardown intent BEFORE issuing `workspace close` |
| DB (teardown intents) | durable intent record: workspace/session id + created_at + state (`issuing`/`issued_ok`/void/consumed) + generation; UNIQUE active intent per workspace (retries supersede, never accumulate); consumed exactly once (CAS); voided on close-command failure; `INTENT_ACK_WAIT_S = 5.0` handler ack-wait; `TEARDOWN_INTENT_TTL_S = 60.0` expiry; additive table (separate from the fatal terminals rebuild); store unavailable → all closes unproven |
| DB (workspace map) | durable `workspace_id → session name`, PK workspace_id; written at workspace creation (herdr backend) + reconcile backfill; create for reused session name RETIRES older rows; close-route completion retires row; sole resolver for unproven close routing; store down → fail-closed unproven/no-op |
| `services/cleanup_service.py` | class-1 fence: retention delete only verified `ready` rows |
| `services/session_service.py` + `services/session_close_service.py` | class-3: cancel+await deferred tasks before core deletion |
| `services/flow_service.py` | class-3: recycle cancels+awaits before `delete_terminals_by_session` |
| Daemon dispatcher (in `terminal_service.py` or a small new module) | service-owned daemon-thread dispatcher: per-call `concurrent.futures.Future`, cancellation-aware async admission (`DEFERRED_EXECUTOR_MAX_WORKERS = 8`, `DEFERRED_ADMISSION_QUEUE_MAX = 32`), deadline-charged slot wait, `deferred_executor_saturated` terminal code |
| Live completion reconciler (same service) | one callback per timed-out MUTATING Future: late H3 terminal → drive settlement; rollback → visible retry; late settle/delete → record result; closed audit-code set (`reconcile_h3_committed` / `reconcile_h3_rolled_back` / `reconcile_settlement_result` / `reconcile_delete_result`); tasks in an INDEPENDENT terminal-service-owned shutdown set (cancel+await; NOT the herdr resolver set); row-gone cancelled delete-callback → visible `reconcile_audit_lost_row_gone`; H5 fallback only on process loss |
| DB (`clients/database.py`) atomic delete helper | `delete_terminal_and_warm_intent(terminal_id, preserve_warm_intent=...)` — ONE transaction deletes terminal row + warm intent (or terminal only when preserving `keep_bases` retention); covers EVERY both-row deletion seam: the separate commits at `terminal_service.py:1834` / `:1837-1842` AND the raw creation rollback `_rollback_terminal_creation` (`:237-251`, swallowed split call removed, `preserve_warm_intent=False`); mid-helper kill/failure rolls both back → row-backed residual owned by init state (pending/failure-terminal → H5/settlement; ready → class-1 cleanup). Startup orphan-auxiliary sweep RETIRED (empty action set): warm intents never swept (lawful retention / CAS-reuse inventory only), transcripts retained, auto-responder non-durable |

## Non-goals

- No warm-sign-in probe (P2c); no init resumption across restarts; no
  out-of-band process-death detector; no delivery/inbox/watchdog changes
  EXCEPT herdr lifecycle CLEANUP seams, which the deletion-authority law must
  correct (codex r5; message delivery semantics stay untouched); sync
  create + rebind validation unchanged; no P5/P6/P8/P4 content (WPM4-B fence).

## Drain shape (post-activation, scratch terminals only)

1. LIVE: plain grok spawn (coin-flip class) reaches ready and receives its task.
2. LIVE: forced never-landing artifact → exactly ONE notice (stable code +
   deadline + token), THEN truthful settlement; row transitions visible.
3. LIVE: healthy fleet restart → zero H5 notices.
4. Suite tier (deterministic clock + stateful seams; the mocked race test at
   `test_wp2s3_start_status_bootstrap.py:696-727` is REPLACED with a seam whose
   delete actually removes metadata):
   - artifact at deadline-minus-one poll → ready; never → one notice;
   - Terminal codes (codex multi-match, identity-invalid) → immediate, no retry;
   - send failure AFTER identity persistence → row still pending → H3 claims it;
   - crash before/after tmux submission and before ready commit → pending →
     restart notice (observability bias), never silently-ready;
   - `TerminalInputBlockedError` queue success (→ ready) and failure (→ H3);
   - crash cutpoints: after H3 commit/before settlement; before commit;
     repeated startup; caller-gone repeated startup (no re-claim);
   - retained-quarantine row across two restarts → settled once, never re-probed;
   - public delete pre-snapshot and mid-sleep → thread-safe cancel+await, leases
     released, core delete after quiesce; quiesce timeout → delete FAILS;
   - internal legacy-path settlement → core seam, no self-cancel deadlock;
   - resume-lease public delete (no `resume_in_progress` strand);
   - cancellation during sleep propagates; caller deleted mid-transaction →
     `init_failed_caller_gone` + audit line;
   - forced inbox insert/flush/commit failure with a LIVE caller → full
     rollback, row stays pending, token unset, ZERO settlement; next sweep
     claims and notifies exactly once;
   - env deadline changed across restart → H5 notice reports the STORED old
     `init_deadline_s`, not current settings; stored deadline stripped from a
     pending row → fail-closed visible skip;
   - settlement raises after H3 commit → claim retained, startup retries
     settlement, audit outcome visible, no second notice;
   - gone-window old-epoch `init_pending` row across restart → startup purge
     SKIPS it; H5 claims, notifies once, settles truthfully (never bare
     `db_delete_terminal`); corrupt/absent init field in purge enumeration →
     skip, fail-closed visible;
   - pre-existing `init_failed_caller_gone` row at startup → audit line
     re-emitted before settlement;
   - one stateful drain per deletion-authority class (not per call site):
     maintenance cleanup (retention + herdr startup) encounters a pending and
     a failure-terminal row → both SKIPPED visibly, only ready rows deleted;
     herdr reconcile/pane.closed observes a vanished pending pane WITH the
     deferred task still live → cancel+await (leases released), then H3
     `worker_vanished` claim, one notice, truthful settlement; class-2
     quiesce timeout → action fails visibly, row left for H5; dual detection
     (poll abort + herdr) → exactly one notice via CAS; intentional
     session/flow teardown with a live deferred task → cancel+await (leases
     released), core deletion, ZERO failure notices;
   - H5 claim busy-exhaustion at startup → startup FAILS visibly (drain +
     mutant asserting the terminal outcome);
   - PROVEN `workspace.closed` (intent committed pre-command, issued_ok,
     consumed once) → class-3, zero notices; UNPROVEN close with a live
     pending worker → class-2 recipe fires (cancel+await, `worker_vanished`
     notice, truthful settle); expired intent (TTL) does not legitimize a
     later close; FAILED close command → intent voided immediately → a
     spontaneous close 5s later is UNPROVEN (class-2 notice, not silent
     class-3); orphaned ABANDONABLE thread completing after quiesce timeout
     → no-op (no claim/settle/delete); orphaned MUTATING thread completing
     late → effects ACCEPTED as truth, invoker issued no competing action
     (grok r9 S2 split);
   - genuine close event arriving while intent is `issuing` → background
     resolver ack-waits, flag flips, consumed as proven class-3 (no false
     notice); still `issuing` at `INTENT_ACK_WAIT_S` → unproven; OTHER herdr
     events (`pane.agent_status_*`) processed DURING the wait (readline
     never stalls); supersede landing mid-wait → consume still targets the
     active `issued_ok` row, single route;
   - session name reused after teardown → late/replayed close for the OLD
     workspace id resolves to a retired map row → visible no-op, recreated
     session's terminals untouched;
   - TWO successful close attempts (finalize_session retry) then one event
     then a later spontaneous close → the retry superseded, one consumption,
     later close finds NO live intent → unproven class-2;
   - uncached unproven close (empty in-memory map, workspace ABSENT from
     live herdr list) with a pending row → durable workspace map still
     routes it class-2; no mapping at all → visible log, no deletion;
   - cancellation during blocked `send_input` → retained concurrent future
     joined before any core delete/H3; cancelled asyncio wrapper does NOT
     remove the registry entry (entry lives until the thread finishes);
   - lifespan shutdown with a blocked executor thread → budgeted join,
     truthful abandon (daemon threads), teardown proceeds AND the process
     EXITS (probe class: no interpreter join on pool workers), pending row
     claimed by H5 on next start;
   - delayed PROVEN close event for retired W1 after S recreated as W2 →
     intent consumed/retired, NO terminal action, pending-W2 untouched
     (universal current-map guard);
   - quiesce timeout while the in-flight future is an H3 claim, and again
     while it is settlement/delete → outcome-unknown reported, no competing
     destructive action, late effects accepted and reconciled from durable
     state;
   - herdr service shutdown mid-ack-wait → resolver cancelled+awaited,
     durable intent left for TTL/recovery;
   - cancellation DURING a blocked capture and DURING a blocked validation
     observation → executor future joined before any core delete/H3 runs;
     executor join exceeding the shared budget → quiesce timeout, core
     deletion skipped, row left;
   - event-loop responsiveness during fresh codex capture (blocking
     sleeps/walks off-loop);
   - env parsing matrix (blank, malformed, 0, -1, nan, inf, 1e309, 700) → WARN + 60.0;
   - migration: backfill atomicity, NOT NULL + vocabulary enforced, corrupt
     pending row → fail-closed visible skip;
   - 9th submission against a saturated dispatcher → loop ticker keeps
     advancing (no threading-semaphore block), queued admission is
     cancellable, slot-wait charged against the call's deadline; no slot at
     deadline → `deferred_executor_saturated`, no fabricated final
     observation;
   - golden timeout-contract pair (same probe shape, different call types):
     ABANDONABLE in flight → exact code `deferred_task_quiesce_timeout` +
     row left; MUTATING in flight → exact code
     `quiesce_timeout_mutation_in_flight` + row outcome unknown;
   - live no-restart late-mutation completion: late H3 success → reconciler
     drives settlement; late H3 rollback (row still pending) → visible
     retry outcome; late settlement/delete → actual result recorded — all
     WITHOUT a server restart (H5 not invoked);
   - process-exit cutpoints inside the real multi-step delete (after window
     kill, after quarantine, INSIDE the atomic terminal+intent helper
     transaction) → each durable residual recovered by its named owner;
     the mid-helper kill rolls BOTH deletes back → terminal row present,
     row-backed recovery, warm_intents orphan count = 0 (the ambiguous
     residual cannot exist); cutpoint owner split by init state: a
     rolled-back helper on an `init_pending` row → H5 claims it; on a
     READY row (public/session delete) → class-1 verified-ready cleanup,
     never H5;
   - raw creation-rollback seam: first-operation (warm-intent delete)
     transient failure inside the helper → whole transaction rolls back,
     `(terminal,warm)=(1,1)` retained, NEVER `(0,1)`; exactly one
     `create_rollback_cleanup_failed` audit line, remaining mechanical
     rollback CONTINUES, and the ORIGINAL create error stays the
     caller-visible error (codex r13 S1); the existing ordering test
     `test_session_brief_contract.py:38-49` preserved/updated for the
     one-seam route;
   - warm-intent preservation set: a lawful `keep_bases` warm intent with
     its tmux session ABSENT (the real post-`finalize_session` state)
     survives untouched — the existing
     `test_session_close_service.py:119-128` retention law holds; ordinary
     worker deletion inside a STILL-LIVE multi-window session removes
     terminal + intent together (no residual, no false preserve); the
     dead-intent CAS-reuse law (`test_epoch_recovery_service.py:58-85`)
     holds; no code path consults `session_exists` for any warm-intent
     decision (backend-error collapse is unreachable); historical
     transcript epochs (two retained) SURVIVE;
   - slot-grant boundary pair: grant recorded at D−ε with validator entry
     at D+ε → counted as the lawful FINAL observation, runs to completion
     past D; grant recorded after D → `deferred_executor_saturated`,
     validator_calls=0; queue-full at submission → immediate saturation;
   - last-observation pin: an INTERMEDIATE grant at D−ε whose observation
     completes at D+δ still-Retryable → processed as the loop's FINAL
     result (deadline-exhausted via H3); NO subsequent same-attempt
     admission, saturation never stacked on a lawful observation;
   - reconciler outcomes golden: each of the four closed audit codes
     asserted exactly; shutdown-at-callback split by outcome: late H3
     (committed/rolled-back) and retained-settlement → task
     cancelled+awaited, H5 recovers on next start; successful DELETE with
     the callback cancelled → row gone, durable state already correct,
     exactly one `reconcile_audit_lost_row_gone` line; reconciler set is
     terminal-service-owned (present and shut down on a non-herdr
     backend);
   - event-loop responsiveness under poll (no starvation).

## Evidence contract (build dispatch embeds this verbatim)

Mutation ledger required; per-mutant artifact = applied diff, exact command,
failing exit + one-line excerpt, post-restore hash. Reviewer scratch-replays a
sample and authors ≥1 own production-path mutant. Named-kill candidates: revert
to single-shot validate; commit ready before initial send; swap H3
commit/settlement order; roll back caller-gone claim to pending; drop caller
snapshot (re-read row); treat codex ambiguous as retryable; sweep on UNKNOWN;
skip settled-state dedup in sweep; re-probe quarantined rows every startup;
external delete skips await (lease leak); internal settlement uses external
wrapper (self-cancel); parse env via bare float(); terminalize a live-receiver
insert failure as caller-gone; H5 reads current settings instead of stored
`init_deadline_s`; make init-schema migration warn-and-continue; let the
startup purge consume non-ready init rows (revert the fence); default an
absent init field to ready in a cleanup enumeration; let retention/herdr
maintenance cleanup delete a pending row; herdr pane.closed raw-deletes a
pending row instead of the H3 `worker_vanished` claim; class-2 settles
without cancel+await (held-lease settlement); session/flow teardown
skips cancel+await (lease leak / task orphan); busy-exhausted H5 sweep
continues startup silently; treat an UNPROVEN `workspace.closed` as class-3
(zero-notice pending consumption); quiesce awaits the task only and skips
the executor-future join; skip the failed-close intent void (TTL-only
lifetime); drop the generation/no-op guard so a late orphaned thread
settles/deletes after quiesce timeout; consume an `issuing` intent without
the ack-wait; insert a NEW intent row per kill_session retry (surplus
authority); resolve an unproven close via the live workspace list instead of
the durable map; join the asyncio wrapper instead of the underlying
concurrent future; remove the registry entry at task done-callback; route
deferred blocking calls through the DEFAULT executor (shutdown hang);
inline-sleep the ack-wait on the readline task (event-loop stall); consume
an intent by stale generation snapshot; skip workspace-map retirement so a
stale row routes against a recreated session; bypass the current-map guard
on the PROVEN path only; use non-daemon executor threads (process exit
blocks); treat an in-flight MUTATING future as abandonable (competing
destructive action during outcome-unknown); acquire the admission
semaphore inline on the event loop (loop stall); fabricate the final
validation observation when saturated at deadline (instead of
`deferred_executor_saturated`); return generic
`deferred_task_quiesce_timeout` for a started MUTATING call; skip the
live completion reconciler so a late-committed H3 claim strands
unsettled until restart; revert `delete_terminal_and_warm_intent` to two
separate commits (re-opens the untracked orphan seam); make the helper
delete the warm intent when `preserve_warm_intent=True` (kills
`keep_bases` retention); add back a startup sweep that retires warm
intents by session-liveness or terminal-row absence (destroys lawful
retention / CAS-reuse inventory); restore the raw creation-rollback
swallowed split call — warm-intent delete fails, terminal delete
succeeds → `(0,1)` orphan + CAS reuse (this mutant must DIE under the
new raw-rollback drain; it survives every pre-r13 drain); swallow a
failed rollback-helper transaction in the outer best-effort boundary
without the `create_rollback_cleanup_failed` audit line; let helper
failure replace the original create error as the caller-visible error; time the final-observation boundary
by validator ENTRY instead of slot grant (suppresses a lawful D−ε final
observation); start the H1 deadline origin at first observation
instead of loop start; schedule a same-attempt admission after a
lawful past-D final observation (stacks saturation on a real result);
register reconciler tasks in the herdr resolver set (reconciler absent
on non-herdr backends); silently drop the row-gone cancelled
delete-callback audit (no `reconcile_audit_lost_row_gone` line).
