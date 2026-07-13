# WPM2 delivery-soundness — archived fold changelogs (r1→r19)

Relocated verbatim from blueprints/wpm2-delivery-soundness.md after FREEZE r21
(8ebd7ca). The blueprint keeps the FREEZE record + the last two rounds inline;
design law was never in this file. Newest first.

r18→r19 changelog (folds codex r18 1B/1S; grok r18 was 0/0/0; codex r18
confirmed CAS feasibility, recovery-vs-recovery, recursive recovery-failure
retry, and all prior closures, but proved the "existing stale-age threshold"
r18 cited does not exist at HEAD — `list_stale_delivering_messages()` is an
unfiltered DELIVERING query, so duration and clock were invented decisions):
- **Pinned stale-open eligibility (codex r18 B1)**: NEW named constant
  `WPM2_STALE_OPEN_AGE_SECONDS = 60`; a row is recovery-eligible iff
  `attempt.started_at <= now_utc − 60s`. The clock is the open ATTEMPT's own
  `started_at` — NEVER inbox message `created_at` (an old PENDING message may
  open a fresh attempt and must not be instantly stale). 60s ≥ 4× the worst
  lawful in-band open lifetime (10s confirmation window + 3×1s settlement
  busy envelope ≈ 13s) and spans two 30s reconciliation periods. Selector
  queries attempt rows by provider + open outcome + attempt age.
- **Return-only control flow (codex r18 S1)**: the settlement helper RETURNS
  `settlement_pending_recovery`; the triggering exception is consumed and can
  never escape into a generic terminal arm.
- Threshold boundary fixtures (−ε / exact / +ε, hours-old message with fresh
  attempt, slow legitimate settlement racing the first eligible pass).

r17→r18 changelog (folds codex r17 1B; grok r17 was 0/0/0; codex r17 confirmed
the exception split is proof-safe and outcome-complete WHEN the compensating
settlement commits, but proved a settlement-transaction failure inside an
exception handler leaves the attempt DELIVERING with startup-only recovery):
- **Settlement-failure law (`settlement_pending_recovery`)**: the proof-safe
  ambiguous-settlement helper uses the existing bounded SQLite busy policy;
  if begin/write/commit still fails it returns the closed result
  `settlement_pending_recovery`, leaving the exact attempt/members DELIVERING —
  it NEVER routes to another terminal arm and NEVER reopens/sends. The nested
  failure is contained: no duplicate paste, no FAILED, no successor.
- **Recurring in-process recovery**: stale-open Claude DELIVERING recovery is
  attached to the existing recurring reconciliation wake, not startup only.
  Past the age threshold it applies the already-pinned w4/D2-first recovery
  law (D2 hit → DELIVERED; receiver-gone → terminal; every other live-receiver
  result → anchor-less PENDING ambiguous/protected), idempotently, via a
  conditional exact open-attempt/member/status CAS so it can never race a
  still-running settlement.
- Settlement-failure injection matrix (BEGIN/evidence UPDATE/member-status
  UPDATE/COMMIT, both exception handlers) + mutant m17 (startup-only recovery —
  a settlement failure with no restart must strand DELIVERING forever and die).

r16→r17 changelog (folds codex r16 1B; grok r16.1 was 0/0/0; codex r16
confirmed the stable-ready matrix total — 72 cases, 0 unclassified — and all
r15 closures, but proved post-submit tail exceptions still reach the generic
FAILED arm):
- **Submit-authority exception split**: the successful-submit seam is the
  authority boundary for claude_code exception routing. After a valid
  anchor/snapshot exists, EVERY later exception (draft restore, provider mark,
  last-active DB write, plugin dispatch, any tail work) settles PENDING
  `ambiguous/confirmation_timeout` persisting the anchor + busy fact
  atomically — the generic FAILED/interrupted/deferred arms are unreachable
  past the submit marker. An exception from the backend submit call itself
  with uncertain acceptance settles anchor-less ambiguous (protected), never
  terminal FAILED; only errors proven to occur before any possible acceptance
  (explicit backend rejection / proven-never-submitted results) retain
  deferred/failed semantics.
- Exception-injection fixture matrix (pre-submit, at-submit uncertain, each
  tail stage) + mutant m16 (restore the generic FAILED arm after the submit
  marker — must terminally fail an accepted paste, lose late-D2 repair, and
  die).

r15→r16 changelog (folds codex r15 1B; grok r15 was 0/0/0; codex r15 confirmed
inbox-1956 itself fully closed but found the adjacent admission-to-submit race):
- `busy_initial_submit` is now DECIDED AT THE SUCCESSFUL BACKEND-SUBMIT SEAM,
  from the same atomic status-lock snapshot that produces
  `injection_completed_seq` — not solely from the earlier S4 admission
  snapshot. Protection rule: non-ready at EITHER observation (admission or
  submit seam) ⇒ busy fact written. `normal` is lawful ONLY under stable-ready:
  both observations ready, same epoch token.
- Fail-closed arms pinned: S4 may not open without a valid atomic admission
  snapshot (unavailable ⇒ hold, no attempt, no send); an unavailable
  submit-seam snapshot or an epoch change between the two observations settles
  WITHOUT a valid anchor, degrading into the already-protected `anchor_missing`
  class.
- Object shape gains `status_at_admission`; coordinated race fixtures
  (ready→PROCESSING-before-paste, PROCESSING→ready-before-paste, both
  unavailable arms) + mutant m15 (decide from admission status only — must
  replay the shifted-observation false loss and die).

r14→r15 changelog (folds codex r14 ADDENDUM 1B — live fixture
`tmp/orch/live-trace-inbox1956-compact-triple-delivery.md`: a /compact
PROCESSING→ready cycle satisfied S1.b while an accepted busy paste sat
queued-but-untranscripted, producing 3 re-pastes + false terminal
delivery_failed on the live server; grok r14 and codex r14 pre-addendum were
both 0/0/0):
- NEW protected class `busy_initial`: every Claude S4 INITIAL injection
  submitted while receiver status ∉ {IDLE, COMPLETED} is, after ambiguous
  settlement, a durable permanently-protected D2-only head. It may exit ONLY by
  S1.a D2 hit (DELIVERED) or D1.1 receiver-gone; it never writes
  `boundary_exhausted_at`, never creates a successor, never consumes cap, never
  terminally fails for absence. Pane status cycles alone are empirically
  insufficient to prove a queued busy paste lost — the S1.b predicate carries no
  cycle-kind fact and compact/lifecycle work is indistinguishable from payload
  consumption.
- New S1.f additive key `busy_initial_submit`, persisted atomically with the
  ambiguous settlement (same transaction as the anchor); crash-before-settlement
  degrades to the anchor-less protected class, so restart cannot lose protection.
- Classifier gains the `busy_initial` permanent reason; protected release law
  unchanged in shape. Exact /compact fixture + mutants m13/m14 pinned.

r13→r14 changelog (folds codex r13 2S; codex r13 was BUILDABLE YES 0B/2S/0N
zero-decision, grok r13 0B/0S/0N — evidence hardening only, no design change):
- Scan-origin table gains two explicit unsupported-provenance rows: malformed
  nested + otherwise-valid top-level, and `cursor_version` present but not
  integer `1` + valid top-level. Both are `unresolved`/non-open — never a
  "nested absent" top-level fallback, never an unversioned origin-0 migration.
- Provenance-stripping pinned as a mutant: HEAD's activity merge writing
  `last_observed_ref` (which would overwrite a versioned cursor with an
  unversioned four-field observation on the next wake) is mutant m12 and a
  named survival test asserts the five-field versioned cursor is byte-equivalent
  across status/transcript activity merges.

r12→r13 changelog (folds codex r12 1B/1S; grok r12 remained 0B/0S/0N YES):
- Cursor provenance pinned: the canonical nested cursor gains `cursor_version: 1`,
  written ONLY by w1/w4/w5. Nested-first authority applies to versioned cursors
  only; unversioned nested state (HEAD liveness writes past unresolved lookups)
  is never an in-transaction baseline and migrates through the out-of-transaction
  refresh (same-identity `min(top_level.size, nested.size)` origin; full rescan
  when only unversioned nested exists). Version-upgrade writes are exempt from
  the unversioned size's monotonic floor. Mutant m10 + over-advanced-legacy
  migration fixture added.
- AdmissionProof-vs-w5 races pinned directly: both commit orders + a
  cursor-substitution-into-stale-proof mutant.

r11→r12 changelog (folds codex r11 1B; grok r11 remained 0B/0S/0N YES):
- `last_observed_ref` is now the sole canonical durable WPM2 continuity cursor;
  nested-first legacy migration is pinned and every D2/admission read uses it.
- `advance_wpm2_continuity_cursor` is the only refresh writer: exact PENDING
  members, same identity, monotonic size, tagged stale/busy/idempotent results.
- Ambiguous and ordinary prior outcomes, concurrent advancement, crash cuts, and
  restart durability after an absent overflow refresh now have named evidence.

r10→r11 changelog (folds codex r10 1B/1S; grok r10 remained 0B/0S/0N YES):
- Transcript admission authority now separates exact identity from append cursor:
  binding/session/path/inode/resolution + payload/window compare exactly;
  `baseline_size` is monotonic and live size growth is parsed, not treated as
  rotation. Truncation, cursor mutation, and real identity change remain stale.
- The hit-between-preflight test proves the appended queued-command bytes are
  actually parsed; size-inequality short-circuit mutants die.
- >1 MiB overflow is explicitly one-wake deferral: out-of-transaction refresh
  either confirms a beyond-cap hit or advances an absent baseline so a later
  opener can proceed. Cap overflow cannot create permanent stale admission.

r9→r10 changelog (folds codex r9 1B/2S; grok r9 remained 0B/0S/0N YES):
- Corrective `AdmissionProof` now fingerprints source payload hash/start window,
  durable binding identity, and continuity reference, then reruns one non-polling
  continuity-aware D2 lookup inside the opener before CAS. Hit, unresolved, or
  authority rotation returns `stale_admission` and never pastes.
- Mixed release evidence puts `anchor_missing`, `epoch_mismatch`, and transient
  heads in one pass ahead of multiple disjoint rows under both selection limits.
- In-transaction transcript work is continuity-offset bounded: one suffix read
  per authority, one parse for all hashes, hard 1 MiB delta cap. Overflow/invalid
  continuity returns stale; whole-transcript scans under `BEGIN IMMEDIATE` are banned.

r8→r9 changelog (folds codex r8 2B; grok r8 remained 0B/0S/0N YES):
- After D1.1, D2 now ALWAYS precedes monitor-snapshot classification. A D2 miss
  plus transient snapshot failure becomes pass-local protection: no permanent
  classification/exhaust/reinject, but D1.3/D8 and disjoint queue release still
  run; the next wake reclassifies from scratch.
- The atomic opener now receives a tagged behavior-specific durable admission
  proof and recomputes its read-set inside the SAME `BEGIN IMMEDIATE` before
  candidate CAS. S4 overlap history, corrective exhausted-source/no-successor,
  and ordinary prior-attempt/transcript authority cannot go stale between
  preflight and open; mismatch returns non-open `stale_admission`.

r7→r8 changelog (folds codex r7 4B; grok r7 remained 0B/0S/0N YES):
- Permanent-D2 classification is total over persisted anchor validation:
  absent/malformed anchors → `anchor_missing`; valid mismatched anchor →
  `epoch_mismatch`; valid same-token → normal; unavailable current monitor
  snapshot → distinct transient stop/retry, never permanent classification.
- Frozen D1.1 receiver-gone settlement is explicitly FIRST for every batch,
  before evidence parsing, classifier, D2, activity/notice, or release scanning.
- The common attempt opener returns a closed tagged result
  `opened(uuid) | delivering_conflict | busy_aborted | stale_candidate`; only
  `opened` may reach backend send. Bare strings/UUID ambiguity is forbidden.
- The opener transaction conditionally CASes the exact receiver-owned candidate
  set PENDING→DELIVERING with rowcount equality before attempt insertion, then
  verifies exact-self. D2/terminal-settlement races cannot resurrect terminal rows.

r6→r7 changelog (folds codex r6 3B/1S; grok r6 S1 converged with codex B1):
- One internal `classify_permanently_d2_only` authority covers ambiguous heads
  with no durable anchor AND anchored heads whose token differs from the current
  monitor token. Both retain PENDING/D2-only proof safety and use the identical
  protected-member notice + skip/release path.
- `skip_d2_only` is emitted only AFTER the protected head's lawful D1.3/D8
  stalled-notice evaluation. Busy-aborted notice transactions stop the wake;
  later D2 confirmation retains the frozen corrective-notice transaction.
- `begin_delivery_attempt_if_no_other_delivering` is now the single attempt-open
  primitive for S4 initial, ordinary initial, and WPM1 corrective injection;
  behavior-specific admission stays outside. Mixed legacy writers are forbidden.
- Release evidence now covers multiple protected sets before multiple disjoint
  rows under default-one and limit-100/grouping selection.

r5→r6 changelog (folds codex r5 3B/2S/1N + grok r5 1S; supervisor
direction pin = proof-safe queue release, not strict-FIFO starvation):
- Anchor-less/crash-recovered D2-only batches remain permanently non-reinjectable,
  but `_handle_wpm1_gate` returns a dedicated `skip_d2_only` result after a D2
  miss; `deliver_pending` excludes that durable member set for the pass and may
  service the next DISJOINT batch. Default-one selection and restart behavior are
  closed below; uncertain heads no longer starve all later callbacks.
- S4 initial admission now searches ALL prior attempt-member sets that overlap
  the candidate. Any overlap not proven never submitted blocks initial paste;
  exact-set regrouping can no longer bypass submission history.
- Epoch tokens are causal only by equality: token mismatch is unconditionally
  D2-only in this slice. Reset/rebind PROCESSING→ready is lifecycle activity, not
  loss proof; no queue-invalidation fact is introduced.
- DELIVERING exclusion is a DB-backed preflight under the delivery lock before
  attempt mutation, plus an atomic open and post-open exact-self invariant.
- Expedited subset wording now records committed SHA `f309165`; S4 explicitly
  supersedes only the Claude initial-readiness seam, never WPM1 D1.4 wholesale.

r4→r5 changelog (folds codex r4 4B/1S; grok r4 already BUILDABLE YES):
- Startup recovery joins the S1 proof-only seam: every stale open Claude
  DELIVERING attempt is conservatively treated as possibly submitted, recovers
  to anchor-less `ambiguous/confirmation_timeout` + `crash_recovery` evidence,
  and remains D2-only PENDING. The old `interrupted/proven_absent` normal-retry
  route is forbidden for this population.
- `observation_epoch` is now an opaque fresh token (UUID) per monitor
  construction and reset/rebind boundary. Tokens are equality-only: same-token
  integer comparisons are lawful. r5's former different-token/current-cycle
  qualification is superseded by r6's unconditional D2-only mismatch law.
- S4 is zero-decision: exact initial-attempt classification, delivery-lock /
  DELIVERING authority, durable-ambiguous FIFO policy (superseded by r6's
  proof-safe release for crash-recovered D2-only heads), and unconditional
  supersession of `EAGER_INBOX_DELIVERY` for Claude initial delivery are pinned.
  Real busy-Claude D5 captures and an unmocked end-to-end parser test are required.
- S4 uses the real D5 vocabulary `empty | nonempty | unresolved`; capture failure
  and parser ambiguity are separately pinned to the fail-closed arm.

r3→r4 changelog (folds codex r3 4B/1S + grok r3 1B; grok B1 ≡ codex B1
convergent — the r3 anchor-writer seam was unlawful at HEAD):
- S1.b/S1.f anchor lifecycle rebuilt (codex B1+B3, grok B1): anchor is marked by
  `mark_injection_completed()` INSIDE the status-lock sequence domain at the
  successful backend-submit seam (post-Enter, before provider/DB/plugin tail);
  `send_prepared_input` returns it; it is carried in memory and persisted
  atomically WITH the ambiguous settlement in `settle_delivery_attempt` (the
  merge helper is NOT used pre-settlement — codex proved that write impossible:
  unsettled attempt, DELIVERING members, merge returns False). Closed crash rule:
  an ambiguous attempt persisted WITHOUT an anchor can NEVER authorize a loss
  (fail-closed pending; D2 poll continues).
- Sequence epochs (codex B2, superseded/closed by r5/r6 token law): every
  persisted/compared sequence is an `{observation_epoch, seq}` pair; monitor
  construction, `reset_buffer`, `clear_terminal`, and rebind each open a fresh
  opaque token; cross-token integer comparison/order is BANNED. r6 further pins
  every token mismatch D2-only; lifecycle cycles never qualify after a change.
- Late-D2 destination (codex B4): terminal-settlement seam gains a closed
  `confirmation_evidence` argument — winning lookup evidence merges into the
  attempt that produced the hit; `terminal_settled_at` into the frozen newest
  target; one transaction; same-row collapse + rowcount/member checks defined.
- `queue_corroboration` encoding closed (codex S1): latest by transcript byte
  offset; `op` ∈ {enqueue, popAll, remove}; `observed_at` = native record
  timestamp when valid, else null (named rule, never invented).
- New tests/mutants: anchor-persist lifecycle, submit→outer-return PROCESSING
  race, restart-between-anchor-and-cycle, reset/rebind epoch, stale-old-epoch
  mutant, old-predicate-restore mutant, initial-hit/late-hit/older-attempt-hit
  trio.

r2→r3 changelog (folds codex r2 3B + grok r2 2S/1N; codex verified S1.a hash
stability, gen-exclusion, 30s reconciliation wake spine, S2 machine, S3):
- S1.b: injection anchor corrected — `injection_completed_seq` captured atomically
  after `send_prepared_input` returns; attempt `last_at` BANNED as boundary (it is
  timeout-settlement time at HEAD, `clients/database.py:1593`); closed non-ready
  set = {PROCESSING}; cycle proof via persisted transition-latch seqs; new
  cycle-during-confirm-window true-loss test (codex B1).
- S1.f NEW: closed additive evidence schema + sole lawful writer through the
  existing `WPM1_EVIDENCE_KEYS` conditional-merge seam; named bypass/wrong-attempt
  mutants (codex B2).
- S1.a: lookups inherit existing continuity-ref semantics (started_at window,
  inode/size continuity) unchanged (grok S2).
- S2: immutable `episode_started_wall_at` pinned as D4 query lower bound; JOIN
  never touches D4 scope, `fired`, `idle_since`, or grace; ordering suppression
  test added (codex B3).
- Snapshot latch keys named explicitly (grok S1).

r1→r2 changelog (folds codex r1 4B/2S + grok r1 3B/4S/2N, both lanes):
- S1 rebuilt: pinned Claude-native consumption oracle (queued_command attachment
  = confirm; codex B1), closed loss-boundary predicate with false-COMPLETED
  exclusion (codex B2, grok B1), atomic boundary-observation object + wake law
  (codex B3), attempt lifecycle pinned to option A — no new outcomes (grok B2),
  D5 inheritance + D1.3/D8 longevity naming (grok S1/S3), backfill firmly OUT
  (codex S1, grok S4).
- S2 rebuilt on the ACTUAL HEAD machine (codex B4, grok B3): emitter pinned,
  spam mechanism = episode replacement on caller SEND_MESSAGE, `fired` latch law,
  escalation arm DELETED, closed busy predicate, D4/FX7 untouched.
- S3: single impl pinned (`stderr=STDOUT`), unique-id dedup + distinct-id
  fail-close, no-trailing-newline fixtures (codex S2, grok N2).
- Commit refs are INNER-repo shas (grok N1).

Origin (evidence-pinned):
- **Incident-1858** (outer `tmp/orch/wpm2-incident-1858.md`): callback to the busy
  long-turn claude_code supervisor injected 3×, every ~13s confirmation window
  expired `transcript_absent`, message falsely settled `delivery_failed`. Codex r1
  empirics on the SAME transcript: the exact wire payload appears as 8
  `queue-operation` records + 3 `attachment.type="queued_command"` records, and
  HEAD `_native_user_turn_texts` (`message_trace_service.py:284-297`) returns ZERO
  matches — the oracle is structurally blind to the form in which busy-receiver
  deliveries actually land. Generation tuples across the three attempts were
  (5,5,5), (6,6,6), (7,6,8): generation correlation advanced across false losses,
  so gens alone are NOT consumption proof.
- **WP2S3 drain C1 FAIL** (`tmp/orch/drain-wp2s3.md` + `c1/`): codex under profile
  `--model`/`-c` emits `session id:` on stderr (stderr capture 3312B contained
  exactly one UUID; stdout 8B, zero); product parses `completed.stdout` only
  (`providers/codex.py:386-391`).
- **Alarm spam**: emitter is `StalledCallbackWatchdog.collect_due_notifications`
  (`stalled_callback_watchdog.py:245-288`). HEAD already dedups per episode
  (`_Episode.fired`, `:46-61`, blocks at `:255-260`); observed spam mechanism is
  episode REPLACEMENT — `InboxService._commit_watchdog_ops` calls
  `record_inbound_task` for caller SEND_MESSAGE while an episode exists
  (`inbox_service.py:189-200`), clearing `fired` and re-arming a fresh alarm
  ~2min after each caller message (codex r1 check 8, worker 89d5dfca history).

