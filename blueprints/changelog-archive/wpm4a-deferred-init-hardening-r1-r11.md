# WPM4-A fold changelog archive — rounds r1-r11 (relocated at FREEZE r14, 2026-07-14)

Verbatim relocation from the blueprint Status header; content-preserving, not law.
Freeze record + r12/r13 remain inline in the blueprint.

Prior: r11 (fold of gate r10: codex FREEZE NO B2/S1/N0 — B1 the
H1 deadline origin was CIRCULAR ("from first observation" before one
exists) and the slot-grant boundary undefined [probe: acquired 45.950ms,
observed 56.026ms vs D=50ms] — folded as origin-at-loop-start +
grant-timestamped boundary (grant ≤ D → lawful final observation,
completes past D; grant > D → saturation, zero validator calls); B2 the
r10 orphan-aux absent-terminal predicate was DESTRUCTIVE [probe: swept a
lawful `keep_bases` 'retain-me' intent; transcript bindings are
append-only history; auto-responder has NO durable rows] — folded as
per-store provenance predicates (warm intents via ready-base tmux-liveness
authority only; transcripts retained; auto-responder excluded); S1
reconciler closed audit-code set + shutdown-set ownership. grok r10:
**FREEZE YES** 0B/3S/2N — first
design-lane freeze; all four codex-r9 folds ruled composing with r4–r9
laws, zero-decision YES. Its pins folded: S1 admission-queued MUTATING
Futures use the mutation-in-flight contract + `deferred_executor_saturated`
joins the closed H3 code map; S2 exclusive live settlement ownership
(Future and/or its single reconciler callback only); S3 orphan-auxiliary
taxonomy = aux-only recovery outside deletion classes 1-3; N4 three new
design-surface rows; N5 `DEFERRED_ADMISSION_QUEUE_MAX = 32`. Codex
empirical r10 pending. Prior: r10 supervisor fold of gate r9: codex FREEZE NO B3/S1/N0
— B1 dispatcher admission undefined: inline threading-semaphore acquire
stalls the loop [probe: 150ms, ticker=0] and a saturated 9th call cannot
meet H1(c)'s exact deadline — folded as cancellation-aware async admission,
slot-wait charged to deadline, `deferred_executor_saturated` terminal code;
B2 generic `deferred_task_quiesce_timeout` row-left contract contradicted
the MUTATING outcome-unknown contract [probe: same_code=False] — generic
code scoped to ABANDONABLE/not-started, atomic wording restricted to the
H3 txn; B3 late-MUTATING reconciliation was optional "(or via H5 next
start)" letting a live server strand a committed claim unsettled [probe:
settled=False, no restart] — folded as the mandatory service-owned live
completion reconciler, H5 fallback only on process loss. Plus S1: named
owner per daemon-exit cutpoint incl. the startup orphan-auxiliary sweep for
terminal-row-gone/warm-intent-live [probe: terminal_rows=0 warm_intents=1].
Prior r9 fold of gate r8: codex FREEZE NO B3/S1/N1 —
B1 the map guard was scoped to UNPROVEN events, letting a delayed PROVEN W1
intent zero-notice delete recreated S/W2 [probe: deleted=['pending-W2']];
B2 `ThreadPoolExecutor.shutdown(wait=False)` does NOT allow process exit with
a running thread [probe: exit blocked until release] — replaced with a
daemon-thread dispatcher; B3 `quiesce_failed` cannot undo a mutation already
executed inside the orphaned Future [probe: row_deleted_inside_future] —
tracked calls split ABANDONABLE vs MUTATING with outcome-unknown semantics.
Plus S1 resolver-task ownership, N1 call-site wording. r9.1: grok r9 delta
FREEZE NO B1 folded — exit-kill atomicity claim NARROWED to the H3 claim txn;
settlement/delete pinned as multi-step with honest partial-teardown residual
+ recovery via H5/class-1/quarantine paths; drain no-op text split
abandonable-vs-mutating; invoker never "finishes the recipe" after
mutation-in-flight timeout. Prior r8 fold of codex r7 B3/S0/N0 —
B1 intent event-before-ack race + retry cardinality [probes: genuine close
routed unproven; 2 close attempts left surplus live authority]; B2 no durable
workspace→session join for uncached unproven closes; B3 the stored
`run_in_executor` Future is CANCELLED with the task [probe: future_cancelled
while thread_finished=False] and 8 `to_thread` sites were uncovered — folded
as issuing-state + one-active-generation intent protocol, durable workspace
map, and the shielded tracked-executor helper + dedicated-executor shutdown
law below. r8.1: grok r8 delta FREEZE NO 2B folded — ack-wait moved OFF the
readline task to a background generation-stamped resolver (one per
workspace); workspace map gained retire/supersede lifecycle for session-name
reuse; consume CAS pinned to the active `issued_ok` row; executor pool size
pinned. Grok r7 delta folded earlier as r7.1. Prior r7 fold of codex r6
B2/S0/N1 —
B1 `workspace.closed` carries NO CAO teardown provenance (wire event =
type+workspace_id only; `kill_session` records no intent) so unconditional
class-3 zero-notice deletion can consume pending workers; B2 cancelling an
asyncio task does NOT quiesce its active `to_thread` executor work
[empirical: task done in 21µs, thread still blocked] — folded as the
teardown-intent record + executor-future quiescence laws below, plus N1
line-249 wording fix. r7.1: grok r7 delta FREEZE NO B1 folded — failed close
command VOIDS the intent immediately (issued_ok flag; TTL alone insufficient
in-window), plus its S2 corrupt-row routing, S3 generation/no-op guard for
orphaned executor threads, S4 late-event accepted dual, N5 intent-store-down
= all closes unproven. Prior r6 fold of codex r5 B1/S2/N1 — the r5 fence
covered ONLY `purge_stale_terminal_records` while herdr
startup/reconcile/pane-close cleanup, retention `cleanup_old_data`, flow
recycle, and session delete/close still raw/core-delete non-ready rows
[empirical: herdr startup cleanup raw-deleted an `init_pending` row]. Folded:
GLOBAL deletion-authority law with three semantic classes, five new design
surfaces, off-loop capture extension, busy-exhaustion drain/mutant, invalid-
identifier terminal behavior). r6.1: grok r6 delta FREEZE NO B1 folded —
class-2 now cancel+awaits the live deferred task (lease release) BEFORE the
H3 `worker_vanished` claim + settlement; class-3 membership pins
(delete_terminal as wrapper, agent_step/script_runner, create-rollback
unwind); dual-detector CAS rule; raw-delete standing rule. Gate artifacts:
tmp/orch/gate-wpm4a-{codex,grok}-r{1..6}.md.
