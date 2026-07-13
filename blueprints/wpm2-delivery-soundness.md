# WPM2 — delivery soundness for busy receivers + alarm hygiene + seed stderr parse

Status: DRAFT r17 (2026-07-13). Micro-WP, three independent slices sharing one gate
train. Builds directly on WPM1 (`8afb758`, FROZEN r9 law) and WP2S3 (`7651dc1`).

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

## S1 — busy-receiver delivery soundness (claude_code)

S1 is a **pure narrowing of WPM1 D1 step 5 (exhaustion eligibility) and step 6
(reinject eligibility), plus a widening of the D2 confirmation oracle's
recognized native shapes**. Cap algebra (total_injections = min(exhausted,2)+1),
D5–D9 arms, outcome/status vocabularies (D7), and the confirm-window settle path
are byte-law unchanged except as stated below.

### S1.a — consumption oracle (closes codex B1)

The Claude-native transcript authority for D2 lookup recognizes, in priority
order, for the exact wire payload hash:

1. Native user-turn text (HEAD behavior, `_native_user_turn_texts`) → **confirmed**
   (existing `transcript_user_turn` kind).
2. `attachment.type == "queued_command"` record whose `attachment.prompt`
   hash-matches the wire payload → **confirmed**, new evidence kind
   `transcript_queued_command`. Rationale: a queued_command record proves the
   harness accepted the injection into the receiver's turn queue; re-injection at
   that point GUARANTEES a duplicate (incident-1858: all three queued copies
   surfaced). Confirming on queue evidence is the strictly safer arm of the
   proof-only law.
3. `queue-operation` records (`enqueue`/`popAll`/`remove`) hash-matching the
   payload are **corroborating evidence only**: recorded into attempt evidence
   JSON when observed, but alone neither confirm nor loss — they lack the
   attachment's stable prompt field contract.

The MSGTRACE RESIDUAL-2 carve-out (no binding → no oracle) is unchanged: S1.a
widens recognized record shapes WITHIN a resolved binding, never the binding
authority itself. S1.a lookups inherit the existing lookup parameters unchanged:
the attempt's `started_at` scan window and the `last_observed_ref`
(path/inode/size/resolution-kind) continuity machinery. Scan origin is unchanged;
the canonical nested storage and legacy precedence rules are pinned in S1.f.

### S1.b — loss-boundary predicate (closes codex B2, grok B1; replaces r1's deferred pin)

A `boundary_exhausted_at` (proven boundary loss) may be written for a
claude_code attempt ONLY when ALL hold:

1. Gate set identical to WPM1 D1.4: receiver status ∈ {IDLE, COMPLETED} AND
   D5 composer tri-state == `empty` (full D1.4/D5 inheritance — mandatory, not
   reopened).
2. **Post-injection turn-cycle evidence** (excludes the incident's false-COMPLETED
   class). Anchor: `injection_completed_seq` — an `{observation_epoch, seq}`
   pair marked by `mark_injection_completed()` INSIDE the S1.c status-lock
   sequence domain at the successful backend-submit seam: immediately after the
   paste/Enter is accepted by the backend, BEFORE the provider-marking/DB/plugin
   tail work inside `send_prepared_input` (codex r3 B3: a fast receiver can
   enter PROCESSING during that tail — the anchor must precede it).
   `send_prepared_input` returns the anchor; if paste/Enter raises with
   acceptance uncertain, no anchor is marked and the attempt settles
   anchor-less PENDING ambiguous per the S1.f submit-authority exception
   split — never terminal FAILED.
   Persistence per S1.f (settlement-time, never pre-settlement).
   Attempt `last_at` is BANNED as the anchor: at HEAD it is timeout-settlement
   time (`settle_delivery_attempt` sets `settled_at = last_at`,
   `clients/database.py:1593`), ~13s after `started_at`, and the incident's real
   queue enqueue/popAll events fell INSIDE that interval — a `> last_at` rule
   would exclude legitimate cycles forever.
   Cycle proof (epoch-aware, codex r3 B2/r4 B2): `observation_epoch` is an
   opaque UUID/token generated as `str(uuid.uuid4())` on monitor construction and on every
   `reset_buffer`, `clear_terminal`, and rebind. Tokens support equality only;
   there is NO ordered/newer-than comparison between epochs anywhere. All
   integer sequence comparisons are lawful ONLY when their token strings are
   equal. Same token as anchor: latches satisfy
   `last_non_ready_seq > injection_completed_seq` AND
   `last_ready_seq > last_non_ready_seq`. **Different token from anchor is
   unconditionally D2-only in this slice**: no PROCESSING→ready pair in a reset,
   reconstructed, or rebound monitor can authorize loss or reinjection, because
   the epoch-opening lifecycle itself can generate that pair. A future exception
   would require a separately gated, durable proof-bearing queue-invalidation
   fact emitted by the epoch-opening operation; WPM2 defines no such fact. A
   stale old-token pair, a mixed old/current pair, and a level-ready current-token
   sample never qualify. Cross-token integer comparison is BANNED. Both the
   anchor and boundary snapshot persist the literal token string, never a hash,
   counter projection, timestamp, or derived ordering key.
   Closed non-ready set: {PROCESSING}
   only — UNKNOWN and WAITING_USER_ANSWER never start a qualifying cycle (they
   are not turn evidence; loss proofs stay conservative). A level sample of
   COMPLETED after injection — the exact incident state — is NOT boundary
   evidence. Generation comparisons over `pre_input_gen`/`pre_status_gen`/
   `settled_status_gen` are NOT sufficient substitutes (empirically falsified:
   tuples above advanced across three false losses).
3. D2 lookup with the S1.a oracle runs FIRST at that observed boundary and
   returns absent. A hit (either confirmed kind) settles confirmed/DELIVERED and
   the batch exits the corrective path (WPM1 D2 unchanged).

Wall-clock expiry alone NEVER writes exhaustion and NEVER authorizes injection.
Non-claude providers: S1 does not apply; their existing WPM1 semantics are
untouched.

**Busy-initial exclusion (codex r14 addendum B1, live trace inbox-1956)**: an
attempt carrying S1.f `busy_initial_submit` NEVER satisfies this predicate —
no same-epoch cycle count, latch state, or absent-at-boundary result can
authorize its loss. Empirical basis: the paste was ACCEPTED into the receiver's
native mid-turn queue while non-ready, but the hash-matching `queued_command`
transcript record is flushed only when the in-flight operation (e.g. /compact)
finishes; meanwhile the operation itself emits same-epoch PROCESSING→ready
cycles with an empty composer and D2 absent — the full r14 predicate — so
status-only turn-cycle proof cannot distinguish lifecycle work from payload
consumption for a busy-admitted paste. Any future relaxation requires a
separately pinned payload-specific queue-invalidation proof; WPM2 defines none.

### S1.c — atomic boundary observation + wake law (closes codex B3)

- One **boundary-observation snapshot object** sampled under a single
  lock/version, with these named fields (grok r2 S1): `observation_epoch`
  (the monitor's CURRENT opaque token string),
  `status`, `status_gen`, `input_gen`, `seq` (monotonic observation sequence),
  plus two transition latches maintained under the SAME lock at observation time:
  `last_non_ready_seq` (seq of the most recent PROCESSING observation) and
  `last_ready_seq` (seq of the most recent {IDLE, COMPLETED} observation).
  Mixed reads of `get_status()` and `get_status_gen()` from different detections
  are forbidden on this path; the snapshot fields are persisted into the attempt
  evidence (S1.f) when and only when they authorize a loss.
- **Wake law**: loss-boundary evaluation is wake-driven as today (published
  status events). Because a same-status ready redraw does not publish
  (`status_monitor.py:257-269`), and queued deliveries flush at receiver turn
  boundaries invisible to the bus, D2 RE-CONFIRMATION (S1.a lookup only, never
  loss-writing) additionally runs on the existing 30-second
  `inbox_reconciliation_daemon` spine (`api/main.py:167-182`,
  `constants.py:140-146` → `reconcile_orphaned_messages` → `deliver_pending`;
  codex r2-verified real) — every wake re-runs D2 before any other arm (D1.2
  ordering unchanged). Liveness clock: confirmation advances on transcript
  flush; loss advances only on observed turn cycles. A receiver that never
  exhibits a post-injection turn cycle keeps the message pending — lawful;
  longevity is covered by **WPM1 D1.3 stalled notice (D8 atomic, once per
  batch) + D9 exempt retention** (NOT the S2 assignment watchdog, which is a
  separate alarm class on worker assignment episodes).

### S1.d — attempt lifecycle + startup recovery (closes grok B2, codex r4 B1)

The confirm window may still settle an attempt `ambiguous`/`confirmation_timeout`
on wall-clock exactly as at HEAD. S1 narrows ONLY D1 step-5 exhaustion
eligibility (S1.b) and step-6 reinject eligibility (exclusively the step-6 path,
now gated on S1.b). No new outcome values, no new message statuses, no new
columns; the only durable additions are the S1.f evidence keys (D7 honored).

`recover_stale_deliveries` is the one widened seam. An open stale DELIVERING
attempt with `provider=claude_code` and no durably settled anchor is
indistinguishable after a crash from one whose backend submit succeeded, because
the submit marker is intentionally in-memory until settlement. Recovery therefore
treats the whole population as **possibly submitted**: transcript hit confirms as
today; every non-hit arm (absent, unresolved/no oracle, or pane temporarily
unresolvable while receiver metadata still exists) settles the attempt and members
to PENDING `ambiguous/confirmation_timeout`, with `crash_recovery` evidence per
S1.f. `_handle_wpm1_gate` recognizes that row as anchor-less and D2-only: it may
confirm later, but can never write exhaustion or authorize a successor paste.
The current `interrupted/proven_absent → normal retry` route is forbidden for this
Claude population. Receiver-gone terminal failure and non-Claude recovery remain
unchanged. Wake selection and D9 gated-PENDING detection semantics are unchanged.

**Permanent D2-only classifier + proof-safe queue release (r7)**:
`classify_permanently_d2_only(attempt, current_observation_epoch)` is the sole
internal classifier. Evidence JSON/anchor validation is explicit and total; it
never raises into delivery. For attempts other than
`ambiguous/confirmation_timeout`, return `normal`. For that outcome, the closed
state table is:

- `anchor_missing` (permanent/protected): evidence JSON is malformed/non-object;
  `injection_completed_seq` is absent/null/non-object; `observation_epoch` is
  missing, empty, or non-string; OR `seq` is missing or not an integer (boolean
  explicitly rejected). This includes every crash-recovered anchor-less attempt;
  `crash_recovery` is explanatory evidence, not required for protection.
- `transient_snapshot_unavailable` (NOT permanent; pass-local protection): anchor is
  valid but the current atomic monitor snapshot/token cannot presently be read.
  It never exhausts, reinjects, or becomes durably classified. After the D2-first
  ordering below, its members are protected/excluded for THIS wake only, ordinary
  D1.3/D8 longevity processing runs, and later disjoint work may proceed. A later
  wake re-runs D2 and classification from scratch.
- `epoch_mismatch` (permanent/protected): anchor is valid, current snapshot/token
  is available, and the literal anchor token differs from the monitor's CURRENT
  token (construction restart, reset, or rebind).
- `busy_initial` (permanent/protected; checked BEFORE the `normal` arm,
  independent of anchor validity/token equality): evidence carries a
  structurally valid `busy_initial_submit` object. The paste was observed
  non-ready at the S4 admission snapshot OR at the backend-submit seam
  (either-observation rule, S1.f); per the S1.b busy-initial exclusion no
  boundary evidence for this attempt is ever sufficient. Exits: S1.a D2 hit or
  D1.1 receiver-gone only. A malformed `busy_initial_submit` object fails closed
  into this same protected reason (never into `normal`).
- `normal`: anchor is valid, current token is available, tokens are equal, and
  no `busy_initial_submit` key is present; normal same-token S1 boundary
  evaluation applies.

All permanent reasons (`anchor_missing`, `epoch_mismatch`, `busy_initial`) are
**permanently D2-only for that epoch/attempt**: never
exhaust, never reinject, remain PENDING until D2 hit/receiver-gone, and share the
same immutable protected-member-set release below. Mechanics are closed:

0. **D1.1 receiver-gone is always first.** Before decoding attempt evidence,
   reading the monitor, classifying, resolving/looking up a transcript, merging
   activity, evaluating D8, or scanning/releasing later rows, verify live receiver
   metadata. Missing metadata atomically settles the exact batch
   `DELIVERY_FAILED/receiver_gone` through the frozen terminal-settlement seam and
   sends its existing caller notice exactly once. No protected-path operation runs.

1. **D2 is always second and monitor-independent.** For every still-live batch,
   decode evidence fail-closed and run the S1.a transcript lookup BEFORE reading
   the monitor snapshot or calling the classifier. A queued-command/native hit
   settles immediately (including frozen delivered-after-stall corrective notice),
   even when every snapshot read would fail. Only a D2 miss proceeds to snapshot
   read/classification.
2. The DB layer adds an oldest-first, cursor/paginated pending scan for one
   receiver that accepts an `excluded_message_ids` set. Unlike
   `get_pending_messages(..., limit=1)`, the scan applies the exclusion in SQL
   BEFORE its requested result limit, so default `num_messages=1` returns the
   oldest non-excluded row; `num_messages=0` returns up to the existing 100
   non-excluded rows. Pagination continues past any number of protected heads;
   existing contiguous sender/orchestration grouping applies only after this
   filtered selection.
3. Under the delivery lock, `deliver_pending` evaluates each oldest ambiguous
   attempt using its durable, immutable member set from
   `inbox_delivery_attempt_member`. After D2 miss, permanent classifier-positive
   heads and `transient_snapshot_unavailable` both enter the non-authorizing
   branch. The gate updates the existing D1.3 activity/stall evidence and
   evaluates D8 exactly-once notice eligibility BEFORE returning. On transient
   snapshot failure no progress observation is invented: preserve durable
   `last_activity_at`, evaluate the absolute-age arm normally, and retry any
   snapshot-dependent idle-age arm next wake. If `record_wpm1_stalled_notice`
   returns `busy_aborted`, the
   whole wake returns generic `stop` so its atomic pair retries; no later batch
   runs on that wake. Otherwise (notice not due, recorded, or already recorded),
   it returns `skip_d2_only(attempt_uuid, member_ids, protection_reason)` instead
   of generic `stop`, where reason is `anchor_missing`, `epoch_mismatch`,
   `busy_initial`, or `transient_snapshot_unavailable`. This is control vocabulary only, not a
   persisted outcome/status. Exhaustion, terminal-failure, successor-begin, and
   backend-send arms are bypassed for the protected set.
4. On `skip_d2_only`, `deliver_pending` adds exactly those member IDs to the
   pass-local exclusion set and selects again. It performs NO `begin_delivery_attempt`,
   no exhaustion write, no status transition, and no backend send for the
   protected set. Generic `stop` retains HEAD's return behavior.
   For the transient reason, exclusion expires at the end of this call and no
   durable permanent/protected marker is written.
5. A later candidate may proceed only when its member set is DISJOINT from every
   protected set and passes the overlap/S4 and DELIVERING preflights below. Its
   attempt chain, cap, hash, and confirmation are independent; servicing it does
   not confirm, exhaust, reorder, or consume the older head's injection budget.
   The protected head stays PENDING in original created-at order and is D2-checked
   again on later reconciliation wakes.

This explicitly supersedes strict terminal-wide FIFO for classifier-positive
permanent D2-only heads and for snapshot-unavailable heads on that wake only. It
preserves WPM1's one-injection and notice/corrective laws per durable member set
while preventing observation failure from starving later callbacks.

### S1.f — closed additive evidence schema + sole lawful writer (closes codex r2 B2)

`WPM1_EVIDENCE_KEYS` (`clients/database.py:1501-1505`) is extended by EXACTLY
these additive keys — nothing else; unlisted keys keep raising
`ValueError("non-WPM1 evidence key")`:

- `injection_completed_seq` (object `{observation_epoch, seq}`): marked in
  memory per S1.b at the backend-submit seam, carried by the delivery path, and
  persisted ATOMICALLY WITH the ambiguous settlement inside
  `settle_delivery_attempt` — one transaction, targeting THE attempt row that
  performed this injection. The `merge_wpm1_attempt_evidence` seam is NOT used
  pre-settlement (codex r3 B1 proved that write impossible at HEAD: attempt
  `outcome=None`, members DELIVERING, merge requires settled ambiguous +
  all-PENDING and returns False). **Closed crash rule**: if the process dies
  between submit and settlement, the attempt settles (or is recovered) WITHOUT
  an anchor, and an anchor-less ambiguous attempt can NEVER authorize a loss —
  fail-closed pending; S1.a/D2 confirmation remains the only exit.
- `crash_recovery` (object `{kind, recovered_at, lookup_kind}`): written only by
  `recover_stale_deliveries` while atomically settling a stale open Claude
  DELIVERING attempt to PENDING `ambiguous/confirmation_timeout`. `kind` is the
  literal `possibly_submitted_without_anchor`; `recovered_at` is the server
  recovery timestamp; `lookup_kind` is the actual startup lookup evidence kind
  (or `transcript_unresolved` when no authority exists). This evidence never acts
  as an anchor and never authorizes loss/reinjection.
- `boundary_snapshot` (object: `{observation_epoch, status, status_gen,
  input_gen, seq, last_non_ready_seq, last_ready_seq}`): written only in the
  transaction that writes `boundary_exhausted_at`, same attempt row, via the
  existing settled-attempt merge seam — a loss without its authorizing snapshot
  is unlawful.
- `queue_corroboration` (object, latest-wins scalar — never an unbounded list):
  `{op, offset, observed_at}` where `op` ∈ closed set {enqueue, popAll, remove};
  "latest" = greatest transcript byte offset among hash-matching records in the
  lookup scan; `observed_at` = the native record's own timestamp when present
  and parseable, else null (named rule — never synthesized from the observation
  clock). Merged during D1/D2 evaluation on settled attempts, existing seam.
- `busy_initial_submit` (object `{status_at_admission, status_at_submit,
  observation_epoch, seq}`): DECIDED at the successful backend-submit seam —
  the same S1.c status-lock point, atomic snapshot, and epoch/seq that produce
  `injection_completed_seq` (`status_at_submit` is that snapshot's literal
  status; `status_at_admission` is the earlier S4 admission snapshot's literal
  status) — carried by the delivery path and persisted ATOMICALLY WITH the
  ambiguous settlement in the SAME `settle_delivery_attempt` transaction as
  the anchor. **Decision rule (codex r15 B1)**: the fact is written when
  EITHER observation is ∉ {IDLE, COMPLETED}; an initial paste is unprotected
  (`normal`-eligible) ONLY under stable-ready — both observations ready AND
  the same epoch token at both points. The admission observation alone is
  NEVER the authority: the delivery lock serializes inbox writers, not
  receiver status, so the receiver may enter PROCESSING between admission and
  paste acceptance. **Fail-closed arms**: if the submit-seam snapshot cannot
  be read, or its epoch token differs from the admission snapshot's, no valid
  anchor is marked and the attempt settles anchor-less — permanently protected
  via `anchor_missing`; if the S4 ADMISSION snapshot cannot be read, S4 does
  not open at all (fail-closed hold: no attempt row, no backend send, retry on
  a later wake). **Closed crash rule**: death between submit and settlement
  loses this fact together with the anchor, and the anchor-less attempt is
  already permanently protected (`anchor_missing`), so protection is never
  lost across restart. This evidence never acts as an anchor and never
  authorizes loss/reinjection; its sole effect is the `busy_initial` protected
  classification.
  **Submit-authority exception split (codex r16 B1)**: the successful-submit
  seam is the authority boundary for ALL claude_code delivery exception
  routing. (i) Once `mark_injection_completed()` has produced a valid
  anchor/snapshot, EVERY later exception in the delivery path — draft restore,
  provider marking, last-active DB write, plugin dispatch
  (`terminal_service.py:1380-1396`), or any other post-submit tail work —
  settles the attempt PENDING `ambiguous/confirmation_timeout`, persisting the
  anchor and any `busy_initial_submit` fact atomically in that settlement; the
  generic FAILED arm (`inbox_service.py:604-613`) and the interrupted/deferred
  arms are UNREACHABLE after the submit marker. (ii) An exception raised by
  the backend submit call itself, where acceptance is uncertain, settles
  anchor-less PENDING `ambiguous/confirmation_timeout` (permanently protected
  via `anchor_missing`) — never terminal FAILED — unless the backend supplies
  an explicit proven-never-submitted result. (iii) Only errors proven to occur
  BEFORE any possible acceptance (the closed proven-never-submitted deferred
  reasons `delivery_deferred`/`input_blocked`, or an explicit backend
  rejection) retain existing deferred/failed semantics. Exits from these
  ambiguous settlements are the standard protected-head exits: S1.a D2 hit or
  D1.1 receiver-gone.
- `kind` gains the new VALUE `transcript_queued_command` (existing key; no new
  key).

**Canonical WPM2 continuity cursor (no schema addition)**:
`last_observed_ref` is THE sole durable cursor object and has the closed shape
`{path, inode, size, resolution_kind, cursor_version}` with `cursor_version: 1`.
Every WPM2 D2 lookup, corrective or ordinary `AdmissionProof`, bounded
in-transaction recheck, and out-of-transaction refresh extracts its baseline from
this nested object; passing the containing evidence object as `expected_ref` or
reading top-level `path`/`inode`/`size` is forbidden. `size` is a non-negative
integer monotonic cursor; `path`/`inode`/`resolution_kind` are its identity and
compare exactly. `cursor_version` is provenance: ONLY w1/w4/w5 write it, always
atomically with the other four fields, and only a cursor carrying
`cursor_version: 1` is a WPM2 parse-authorized baseline. A nested object WITHOUT
`cursor_version` is legacy liveness state (HEAD's activity merge stats the
transcript and writes `last_observed_ref` even after an UNRESOLVED lookup —
`inbox_service.py:258-296` — so its size may sit past bytes D2 never parsed) and
is NEVER an in-transaction baseline; structural validity does not establish
provenance.

Legacy read precedence is closed BY PROVENANCE, not structure. A VERSIONED
`last_observed_ref` always wins and every other cursor source is ignored even
when it conflicts. All unversioned states route to the out-of-transaction
refresh (in-transaction they are `stale_admission`, per the no-valid-baseline
rule), with a closed scan-origin table:
- unversioned nested + complete valid legacy top-level, SAME
  `{path,inode,resolution_kind}` identity → scan origin
  `min(top_level.size, nested.size)`;
- unversioned nested alone (top-level absent/invalid), identity intact → scan
  origin 0 (full rescan, still subject to the refresh path's own bounds);
- top-level alone (nested key absent) → the one-wake seed: scan origin
  `top_level.size`;
- malformed nested (structurally invalid object under the key) + otherwise-valid
  top-level → `unresolved`/non-open — a present-but-broken nested cursor is
  unsupported provenance, NOT "nested absent", so the top-level seed arm never
  applies;
- nested `cursor_version` present but not integer `1` (any other type or value)
  + valid top-level → `unresolved`/non-open — an unknown version is neither
  versioned authority nor legacy unversioned state; future version handling must
  extend this table explicitly, never silently reinterpret;
- identity conflict between any two sources, or all sources missing/malformed →
  `unresolved`/non-open — never fallback.
A hit found during any of these scans may settle immediately; an absent scan
must persist the returned VERSIONED cursor through
`advance_wpm2_continuity_cursor` before any AdmissionProof/open. Once a
versioned cursor is persisted, legacy top-level fields and unversioned nested
state are never consulted again.

All WPM2 settlement paths initialize the nested cursor (versioned, atomically
with all five fields) from the attempt's captured continuity reference: w1
covers normal ambiguous and ordinary non-success settlement, and w4 covers
crash recovery when no normal settlement committed.
If no valid authority can be captured they persist no cursor and the row remains
unresolved/non-open. HEAD's activity merge may still write
`last_observed_status`/`last_activity_at`, but it MUST NOT write
`last_observed_ref`; a fully parsed absent lookup delegates that write to w5.

**Confirmation-evidence destination (closes codex r3 B4)**: the terminal
settlement seam (`settle_wpm1_terminal_batch`) gains a closed
`confirmation_evidence` argument. Target rule: the winning lookup evidence
(including `kind=transcript_queued_command`) merges into the exact attempt that
produced the hit; `terminal_settled_at` merges into the frozen newest-attempt
target — both in the SAME transaction. When hit-attempt == newest target, the
two merges collapse into one row update. Existing rowcount/member-set checks
apply to both writes; lookup evidence is never discarded (`_handle_wpm1_gate`'s
current `lookup_result, _` discard is corrected as part of this wiring).

Lawful writers, complete list: (w1) `settle_delivery_attempt` extended to
persist the in-memory anchor and initialize the canonical cursor with settlement;
(w2) the existing settled-attempt conditional-merge seam for `boundary_snapshot`/
`queue_corroboration`; (w3) the extended terminal-settlement transaction for
confirmation evidence; (w4) `recover_stale_deliveries` writing `crash_recovery`
and initializing the cursor in the same settlement transaction that creates the
closed anchor-less ambiguous row; (w5) `advance_wpm2_continuity_cursor` updating
ONLY the existing `last_observed_ref` key under the cursor transaction below. No
other writer, no direct row UPDATE; HEAD
PENDING/member-set/rowcount/busy semantics unchanged everywhere except as
stated in w1–w5. Named mutants (must die): (m1) write a new key bypassing the
allowlist; (m2) write anchor or `boundary_snapshot` to a different attempt row
than the injecting/exhausting one; (m3) write `boundary_exhausted_at` without
`boundary_snapshot` in the same transaction; (m4) restore the pre-settlement
merge predicate for the anchor (must fail the anchor-persist lifecycle test);
(m5) authorize a loss from an anchor-less attempt; (m6) compare sequences
across epochs; (m7) restore stale-Claude `interrupted/proven_absent` recovery
and normal reinjection; (m8) mutate a cursor only in memory; (m9) directly UPDATE
attempt evidence instead of using w5; (m10) trust an unversioned nested size as a
scan baseline (must skip the unparsed interval, duplicate, and die); (m11)
substitute a newer cursor into an already-built AdmissionProof instead of
rebuilding the full proof; (m12) restore HEAD's activity-merge cursor write
(`updates["last_observed_ref"] = observation`, `inbox_service.py:271-296`) —
the unversioned four-field observation overwrites a versioned cursor on the
next wake, stripping provenance; must die against the cursor-survival test;
(m13) treat a same-epoch PROCESSING→ready cycle as loss proof for an attempt
carrying `busy_initial_submit` (must replay the inbox-1956 triple-paste +
false terminal failure and die against the /compact fixture); (m14) carry the
busy-initial fact in memory only / skip persisting it in the settlement
transaction (restart must forget protection, exhaust, and die); (m15) decide
`busy_initial_submit` from the admission observation ONLY (a
ready→PROCESSING-before-paste receiver classifies `normal`, replays the
shifted-observation false loss, and dies against the coordinated race
fixture); (m16) restore the generic FAILED arm after the submit marker (a
post-submit tail exception terminally fails an accepted paste, losing late-D2
repair — must die against the submit/tail exception matrix).

`advance_wpm2_continuity_cursor(attempt_uuid, exact_message_ids, expected_ref,
observed_ref)` uses the frozen `_run_wpm1_immediate` 3×1s transaction policy and
returns the closed tag `advanced | already_advanced | stale | busy_aborted`.
Inside `BEGIN IMMEDIATE` it re-reads the named attempt and its EXACT member set,
requires every member still PENDING, and requires a settled, non-success attempt
currently eligible as either (a) any `ambiguous` row, including WPM1
confirmation-timeout/crash recovery, or (b) an ordinary prior-outcome row named
by the current WPM2 read-set, including `interrupted` and proven-never-submitted
`deferred`. Delivered/confirmed rows, ALL failed-send rows, open attempts,
unrelated outcomes, changed members, non-PENDING members, or changed rowcount
return `stale`; no row is written and the caller stops this candidate for the
wake.

The transaction decodes the stored canonical cursor (or an unversioned/legacy
state under migration) and requires observed identity `{path,inode,
resolution_kind}` to equal both stored and expected identity with observed
`size >= expected size`. Normally the stored object equals `expected_ref`. If a
concurrent writer already moved the same identity beyond expected, a VERSIONED
stored size >= observed size returns `already_advanced`; versioned stored size
between expected and observed advances to observed. Identity change, versioned
stored size below expected, malformed state, or observed regression is `stale`.
VERSION-UPGRADE writes are the one pinned exemption: when the stored state is
unversioned (or a top-level-only seed), its size is NOT a monotonic floor — the
migration scan's `expected_ref` derives from the closed scan-origin table above,
and w5 overwrites the unversioned state with the versioned result even when the
new size is smaller (an unversioned size may be over-advanced past unparsed
bytes; trusting it as a floor would re-skip them). Monotonicity binds strictly
WITHIN versioned cursors. The successful read-modify-write changes only
`evidence.last_observed_ref` and preserves every other evidence key/value.
Concurrent versioned writers therefore converge on max size and can never move
the cursor backward or across identity.

For grouped ordinary reads, call w5 oldest-first once per eligible attempt whose
full out-of-transaction scan returned absent. Any `stale`/`busy_aborted` stops
opening on that wake; already-committed earlier advances remain safe and the next
wake resumes the rest. `advanced`/`already_advanced` permit fresh preflight only —
they never themselves authorize send or absence. Crash before commit leaves the
old cursor and causes a safe rescan; crash after commit reloads the new cursor on
restart. No backend send occurs between refresh scan and cursor commit.

### S1.e — historical backfill: OUT (closes codex S1, grok S4)

No retroactive repair of pre-WPM2 `delivery_failed` rows in this slice. Recorded
as a residual; any future backfill is its own gated slice.

### S1 evidence bar (tests derive from THIS text)

- **Incident inversion (PRIMARY)**: receiver held busy across ≥3 confirmation
  windows after one real injection; transcript receives queued_command
  attachment (real incident record shape as fixture) → exactly 1 injection, 0
  loss proofs, 0 delivery_failed; D2 confirms `transcript_queued_command` →
  DELIVERED; attempt chain length 1.
- **False-COMPLETED exclusion**: status latched COMPLETED post-injection with NO
  turn cycle, payload absent → no exhaustion written, no reinject, message
  pending (gen tuples may advance, mirroring (5,5,5)/(6,6,6)/(7,6,8) — test
  asserts they do NOT authorize).
- **True loss**: observed non-ready→ready cycle post-injection, S1.a lookup
  absent at that boundary → exactly one proven loss; WPM1 cap algebra proceeds
  unchanged to its existing arms.
- **Cycle-during-confirm-window true loss (codex r2 B1)**: real cycle occurs
  after `injection_completed_seq` but BEFORE the confirmation window expires →
  the cycle still qualifies (anchor is injection completion, not settlement);
  loss provable on the next evaluation. Anti-test: the same events anchored on
  `last_at` would wrongly disqualify — asserts the ban.
- **S1.f schema law**: named mutants m1–m16 all die; unlisted-key write raises.
- **Submit/tail exception matrix (codex r16 B1)** — exception injected at each
  pinned stage with exact status/outcome/evidence assertions:
  `test_wpm2_pre_submit_exception_keeps_deferred_failed_semantics` (error
  proven before any possible acceptance → existing deferred/failed arms
  unchanged);
  `test_wpm2_submit_exception_uncertain_acceptance_settles_anchorless_ambiguous`
  (raise inside/at return from the backend submit call → anchor-less PENDING
  ambiguous, `anchor_missing` protected, never FAILED; if the paste actually
  landed, late D2 confirms DELIVERED);
  `test_wpm2_tail_exception_after_anchor_settles_ambiguous_with_fact` — one
  test per tail stage (draft restore, provider mark, last-active DB write,
  plugin dispatch): attempt settles PENDING ambiguous with anchor +
  `busy_initial_submit` persisted atomically, at most one paste, restart cut
  preserves protection, late queued-command suffix hit settles DELIVERED with
  chain length 1. m16 (generic FAILED restored past the submit marker) dies
  across this matrix.
- **Busy-initial /compact fixture (codex r14 addendum B1, live trace
  inbox-1956)** — `test_wpm2_busy_initial_compact_cycles_never_exhaust`: one S4
  paste admitted while PROCESSING (accepted into the native queue,
  `queued_command` record deferred), then repeated same-epoch PROCESSING→ready
  cycles with empty composer and D2 absent across multiple wakes → NO
  `boundary_exhausted_at`, NO successor attempt, NO cap consumption, NO
  terminal failure; then a late queued-command suffix hit settles DELIVERED
  with attempt-chain length exactly 1. m13 replays the recorded live outcome
  (3 pastes + `delivery_failed`) and dies. m14 (in-memory-only busy fact) dies
  across a restart cut between settlement and the next wake.
  `test_wpm2_busy_initial_receiver_gone_exit` pins the only other exit;
  `test_wpm2_stable_ready_initial_keeps_normal_discipline` asserts a paste
  ready at BOTH observations under one epoch carries no `busy_initial_submit`
  and remains subject to normal S1.b loss proof; a malformed
  `busy_initial_submit` object classifies protected (fail-closed), never
  `normal`.
  **Admission-to-submit races (codex r15 B1)** —
  `test_wpm2_ready_admission_processing_before_paste_is_protected`: admission
  snapshot COMPLETED, receiver enters PROCESSING before backend paste
  acceptance, submit-seam snapshot non-ready → fact written, one paste,
  PENDING through subsequent same-epoch compact cycles, late suffix hit →
  DELIVERED, chain length 1; m15 (admission-only decision) classifies it
  `normal`, replays the false loss, and dies.
  `test_wpm2_processing_admission_ready_before_paste_still_protected`: the
  mirror race (busy at admission, ready at submit seam) also writes the fact —
  EITHER-observation rule.
  `test_wpm2_s4_admission_snapshot_unavailable_holds`: no attempt row, no
  backend send, no mutation; later wake retries.
  `test_wpm2_submit_snapshot_unavailable_or_epoch_change_settles_anchorless`:
  both arms settle without a valid anchor and classify `anchor_missing`
  (protected); no loss proof is ever available to them.
- **Cursor compatibility + writer matrix**:
  `test_wpm2_versioned_nested_cursor_wins_over_conflicting_legacy_top_level`,
  `test_wpm2_legacy_top_level_cursor_migrates_once`, and
  `test_wpm2_malformed_nested_cursor_never_falls_back` pin precedence.
  `test_wpm2_unversioned_nested_over_advanced_by_unresolved_lookup_migrates`
  (codex r12 B1 fixture): a HEAD-style activity merge advanced the unversioned
  nested size past a valid queued-command record while top-level stayed earlier
  on the same identity → migration scans from `min(top_level.size, nested.size)`,
  parses the skipped interval, and CONFIRMS the hit; m10 (trusting the
  unversioned nested size) misses the record, duplicates, and dies.
  `test_wpm2_unversioned_nested_alone_full_rescan` pins the origin-0 arm and
  `test_wpm2_version_upgrade_writes_smaller_size` pins the monotonic-floor
  exemption (versioned result smaller than the unversioned state commits).
  **Unsupported provenance rows (codex r13 S1)** —
  `test_wpm2_malformed_nested_with_valid_top_level_unresolved` and
  `test_wpm2_unknown_cursor_version_with_valid_top_level_unresolved`: both
  fixtures carry a valid legacy top-level cursor and must return
  `unresolved`/non-open — asserting neither the top-level seed arm nor the
  unversioned origin-0 arm fires when a nested object is present but broken or
  carries an unsupported version.
  **Provenance survival (codex r13 S2)** —
  `test_wpm2_versioned_cursor_survives_status_and_transcript_activity_merge`:
  after a versioned cursor is persisted, ordinary activity processing updates
  `last_observed_status`/`last_activity_at` while the five-field versioned
  cursor stays byte-equivalent; m12 (restoring the HEAD activity-merge
  `last_observed_ref` write) must die here.
  `test_wpm2_advance_cursor_ambiguous_and_interrupted_rows` covers both eligible
  populations and proves failed/open/terminal rows are stale.
  `test_wpm2_advance_cursor_rowcount_stale_and_busy_results` pins the four result
  tags, exact members, all-PENDING predicate, and 3×1s busy stop.
  `test_wpm2_concurrent_cursor_advances_converge_on_max_size` coordinates two DB
  connections in both commit orders; identity never changes and size never
  regresses. Crash fixtures before and after the cursor commit prove respectively
  safe rescan and durable restart reuse. In-memory-only mutation and direct
  evidence-UPDATE mutants must fail this matrix.
  **Proof-vs-w5 races (codex r12 S1)** —
  `test_wpm2_admission_proof_vs_w5_advance_both_commit_orders`: (a) proof
  captured at baseline N, concurrent w5 advances the durable cursor to M before
  the opener transaction commits → opener revalidation returns `stale_admission`,
  zero send, a later wake rebuilds a fresh proof; (b) opener commits first →
  the later w5 sees the candidate no longer PENDING and returns `stale` with no
  cursor mutation. m11 (splicing the newer cursor into the stale proof instead
  of rebuilding) must die in arm (a).
- **Anchor lifecycle (codex r3 B1)**: begin → submit-seam mark → ambiguous
  settlement persists anchor atomically; m4 (pre-settlement merge predicate
  restored) fails this test. Crash-cut: kill between submit and settlement →
  anchor-less attempt → no loss ever authorized (m5 dies).
- **Submit race (codex r3 B3)**: PROCESSING observation injected AFTER
  backend submit but BEFORE `send_prepared_input` returns → cycle still
  qualifies (anchor marked at submit seam, not at return).
- **Epoch law (codex r3 B2/r5 B3)**: construction/reset_buffer/rebind open fresh
  opaque tokens; same-token integer comparison may authorize only the complete
  anchored cycle above, while every token mismatch is D2-only. Rebind/reset
  initialization may complete PROCESSING→ready with the payload transcript-absent
  and still writes NO loss/reinject authorization. Old-token snapshot against a
  current-token monitor and mixed old/current latch pairs do not qualify; m6
  (ordered/cross-token compare or mismatch-cycle authorization) dies.
- Named token tests: `test_wpm2_old_token_snapshot_cannot_qualify_current_monitor`,
  `test_wpm2_mixed_token_latches_cannot_qualify`, and
  `test_wpm2_rebind_reset_cycle_with_absent_payload_stays_d2_only`. All three are
  old-token/current-token mismatch pins; the last uses the real rebind/reset
  ordering and kills authorization from its initialization cycle.
- **Classifier totality (codex r7 B1)**: table-driven
  `test_wpm2_permanent_d2_classifier_validation_matrix` names and asserts every
  row: evidence JSON malformed; anchor absent; anchor non-object; epoch missing;
  epoch empty/non-string; seq missing; seq non-integer/bool → `anchor_missing`;
  valid anchor + unavailable monitor snapshot → `transient_snapshot_unavailable`;
  valid mismatch → `epoch_mismatch`; valid same-token → `normal`. Separate
  ordering/liveness tests close its behavior:
  `test_wpm2_transient_snapshot_failure_d2_hit_confirms_immediately` supplies an
  existing queued-command hit and proves snapshot/classifier are never called;
  `test_wpm2_repeated_transient_snapshot_failures_release_disjoint_callbacks`
  proves repeated failures write no exhaustion/attempt/cap state for the head,
  run longevity, and let later disjoint callbacks inject once; and
  `test_wpm2_transient_recovery_same_token_resumes_without_cap_consumption`
  restores the snapshot on a later wake and resumes normal same-token evaluation
  with the original attempt count/exhaustion budget unchanged. Mutations reading
  snapshot before D2 or mapping transient to generic terminal-wide `stop` die.
- **Receiver-gone precedence (codex r7 B2)**:
  `test_wpm2_receiver_gone_precedes_malformed_protected_evidence` and
  `test_wpm2_receiver_gone_precedes_ordinary_protected_evidence` remove receiver
  metadata before the wake. Each settles `receiver_gone` exactly once and asserts
  classifier, transcript/D2, activity merge, stalled notice, and release scan are
  never called. Moving D1.1 after any protected operation must die.
- **Mismatch release liveness (codex r6 B1/grok S1)**:
  `test_wpm2_construction_restart_protects_mismatch_and_releases_disjoint_callback`
  and `test_wpm2_rebind_reset_protects_mismatch_and_releases_disjoint_callback`
  persist an anchored ambiguous head under the old token, open the new monitor
  token, keep the head PENDING with zero reinjection/loss, and inject a later
  disjoint callback exactly once. Mutating either classifier reason to generic
  `stop` must starve the later row and die.
- **Crash recovery (codex r4 B1)**: real injection submit succeeds, process dies
  before anchor settlement, startup recovery runs, then multiple reconciliation
  wakes occur before queued-command confirmation → exactly ONE total injection,
  no loss proof, closed PENDING until D2 confirms. m7 restoring
  `interrupted/proven_absent → normal reinject` must produce a second paste and die.
  Named test: `test_wpm2_crash_recovery_stays_d2_only_across_reconcile_wakes`;
  the m7 mutation is killed by that same injection-count assertion.
- **Crash cuts + queue release (codex r5 B1/S1)**:
  `test_wpm2_crash_before_submit_protects_head_and_releases_disjoint_callback`
  kills the process after `begin_delivery_attempt` but before backend submit;
  `test_wpm2_crash_after_submit_protects_head_and_releases_disjoint_callback`
  kills after submit but before settlement. Both recover the head D2-only, never
  paste it again, and inject a later disjoint callback exactly once. Named liveness
  test `test_wpm2_permanently_absent_head_does_not_starve_later_callbacks` runs
  multiple reconciliation wakes: the head remains PENDING/absent with one total
  injection maximum, while each later disjoint callback progresses under its own
  one-injection chain. Removing SQL exclusion-before-limit, mapping
  `skip_d2_only` back to generic `stop`, or calling begin/send for excluded IDs
  must fail these tests.
- **Protected-head notice ordering (codex r6 B2)**:
  `test_wpm2_protected_head_stalled_notice_once_before_skip` crosses both D1.3
  thresholds across repeated wakes and records exactly one D8 notice before
  release; `test_wpm2_protected_head_notice_busy_abort_stops_whole_wake` forces
  `record_wpm1_stalled_notice=busy_aborted` and proves no later callback runs on
  that wake; `test_wpm2_protected_head_late_delivery_emits_corrective_notice`
  first records the stall, then adds a D2 queued-command hit and verifies the
  frozen delivered-after-stall corrective notice transaction exactly once.
  Returning `skip_d2_only` before D1.3/D8 must kill all three.
- **Multi-head release and limits (codex r6 S1)**: two protected durable member
  sets precede multiple disjoint rows. Named tests
  `test_wpm2_default_one_skips_all_protected_sets_before_each_disjoint_row` and
  `test_wpm2_limit_all_excludes_protected_sets_before_grouping` exercise
  `num_messages=1` across repeated calls and `num_messages=0`/limit-100 with
  contiguous sender/orchestration grouping. Every protected member stays out of
  later candidate groups; every disjoint row injects once. One-exclusion-only,
  exclusion-after-LIMIT, and protected-member-regrouping mutants must die.
- **Mixed permanent/transient single-pass release (codex r9 S1)**: one queue has
  interleaved `anchor_missing`, `epoch_mismatch`, and
  `transient_snapshot_unavailable` heads before multiple disjoint rows. Named
  tests `test_wpm2_default_one_mixed_protection_checks_and_releases_in_order` and
  `test_wpm2_limit_all_mixed_protection_excludes_before_grouping` exercise
  default `num_messages=1` and `num_messages=0`/limit-100 in a SINGLE call/pass.
  They assert every head receives D2 oldest-first; all three member sets are
  excluded before selection/grouping; no protected member enters any later group;
  every disjoint row injects exactly once; only the transient exclusion disappears
  at call end while permanent classification remains derivable on the next wake.
  Mutants partitioning permanent/transient scans, expiring every exclusion,
  retaining transient exclusion across calls, or grouping before all exclusions
  must die.
- **Confirmation destination (codex r3 B4)**: initial-hit, late-hit with one
  attempt, late-hit landing on an OLDER attempt than the newest settlement
  target — evidence rows land per the S1.f target rule in all three.
- **Never-cycling receiver**: no delivery_failed within any horizon; D1.3/D8
  stalled notice exactly once; D9 retention holds.
- **Oracle priority**: native user-turn AND queued_command both present → one
  confirm, kind = `transcript_user_turn` (priority 1); queue-operation records
  alone → neither confirm nor loss, corroboration recorded.
- **Wiring mutants (named)**: sever S1.b gating from step-5/step-6 (must die);
  stale/mixed-snapshot mutant — status from one detection, gen from another
  (must die); same-status-redraw-publishes mutant vs D2-poll liveness (must die).

## S2 — assignment-watchdog alarm hygiene (closes codex B4, grok B3)

Emitter pinned: `StalledCallbackWatchdog.collect_due_notifications`
(`stalled_callback_watchdog.py:245-288`) — assignment episodes only. FX7
`_waiting_inbox_episodes` machinery and WPM1 D4 suppression are **byte-untouched**;
D4 (in-flight deferred/ambiguous episode callback blocks fire) remains
authoritative whenever it applies, and S2 hygiene governs only fires that pass D4.

Laws:

1. **Episode identity (kills the replacement-spam class)**: an assignment
   episode spans from the FIRST `record_inbound_task` until callback observed or
   explicit clear. The episode carries an IMMUTABLE `episode_started_wall_at`
   (set once at episode start), and the frozen D4 suppression query's lower
   bound is pinned to THAT field — so a PENDING callback created any time after
   the first assignment stays visible to D4 for the episode's whole life
   (codex r2 B3). A caller SEND_MESSAGE arriving while the episode is active
   and unanswered JOINS it: recorded as informational `last_join_wall_at` only;
   it does NOT reset `fired`, does NOT move the D4 bound, does NOT reset
   `idle_since` or the grace clock, and does NOT create a fresh alarm-eligible
   episode. A new episode (and thus new alarm eligibility) begins only after
   the prior episode ended (callback/clear).
2. **`fired` latch**: at most one alarm per episode, latched until episode end.
   No active→idle re-fire within an episode (HEAD's observed behavior — codex
   probe: first=1, after_active_idle=0 — is RATIFIED as law). **Escalation arm:
   deleted** — no threshold in this slice.
3. **Busy suppression (closed set)**: no alarm while status ∉ {IDLE, COMPLETED}
   OR the existing screen-fingerprint liveness shows change within the grace
   window. No new busy oracle is introduced.

Evidence bar:
- **Incident-shaped (PRIMARY)**: episode active, worker mid-flight, ≥3 caller
  SEND_MESSAGEs arrive before any callback → exactly 1 alarm total.
- Once-per-episode: continuous alarm-eligible state across ≥3 poll windows → 1.
- New-episode re-arm: callback ends episode; new `record_inbound_task` → next
  alarm eligible (exactly 1 more).
- Busy/fingerprint suppression: unstable screen_fp or non-ready status → 0.
- D4 precedence: PENDING in-episode callback → 0 (existing D4 tests remain
  green, unmodified).
- **Join-ordering suppression (codex r2 B3)**: callback row created AFTER first
  assignment but BEFORE a later joined SEND_MESSAGE → D4 still suppresses (the
  immutable `episode_started_wall_at` bound holds).
- **Wiring mutant (named)**: re-enable `fired` reset on caller SEND_MESSAGE
  (the HEAD bug) — the PRIMARY test must fail (kill).

## S4 — claude_code delivery latency (busy-injection, NEW in r4)

Origin (user pain, 2026-07-13, same session as incident-1858): worker callbacks
to a busy supervisor arrive minutes late — the delivery gate waits for ready
status, so total latency ≈ the receiver's own turn length (observed ~9 min for
the codex r3 gate verdict). The receiver's harness has a NATIVE mid-turn queue
(the queued_command mechanism S1.a confirms against); waiting for ready is
unnecessary for claude_code.

**Law**: for claude_code receivers, INITIAL injection of a message no longer
requires receiver status ∈ {IDLE, COMPLETED}. It requires D5 composer tri-state
== `empty` (inherited unchanged — never paste over a draft; `nonempty` and
`unresolved` hold fail-closed) and the concurrency/queue authorities below.
This supersedes ONLY the WPM1 D1.4 **initial-injection readiness seam** in HEAD
`deliver_pending` for the first Claude paste; it does NOT supersede D1.4
wholesale. S1.b.1's ready ∩ empty gate for loss proof and corrective reinjection
remains mandatory and byte-law intact. The initial ready gate existed because
busy delivery was unconfirmable under the proof-only law; S1.a makes it
confirmable (queued_command evidence). CORRECTIVE re-injection
(the D1 step-6 path) keeps the FULL S1.b boundary discipline — S4 never
relaxes loss proofs or reinjection, only the first paste.

**Busy-initial protection (codex r14 addendum B1; race-closed at the submit
seam per codex r15 B1)**: S4 requires a valid atomic admission snapshot before
opening — unavailable ⇒ fail-closed hold (no attempt, no send, later-wake
retry). The S1.f `busy_initial_submit` fact is then DECIDED at the successful
backend-submit seam from the same atomic snapshot that produces the anchor:
non-ready at EITHER the admission or the submit-seam observation ⇒ the fact is
written and, from ambiguous settlement on, the attempt is a permanent
`busy_initial` protected D2-only head: confirmable by S1.a, settleable by D1.1
receiver-gone, subject to ordinary D1.3/D8 stalled-notice longevity — but it
never writes `boundary_exhausted_at`, never begins a successor, never consumes
injection cap, and never settles terminally for absence. The `queued_command`
transcript record may be flushed arbitrarily late (only when the receiver's
in-flight operation completes), so absence during that window is not evidence.
Unavailable submit-seam snapshot or epoch change between the two observations
⇒ anchor-less settlement (`anchor_missing`, equally protected). ONLY a
stable-ready initial paste — ready at admission AND at the submit seam, same
epoch token — carries no `busy_initial_submit` and keeps the full normal S1
discipline; a receiver that flips ready→PROCESSING between admission and paste
acceptance is busy-protected exactly like a busy-admitted one (one paste,
PENDING until late D2 confirmation or receiver-gone).

**`Initial` classification with overlap safety**:

- Before S4 admission, `list_overlapping_attempts(candidate_message_ids)` queries
  durable attempt membership for ALL prior attempts
  whose member set intersects the candidate member set. No overlaps, or overlaps
  consisting ONLY of `deferred` attempts with the closed proven-never-submitted
  reasons `delivery_deferred` / `input_blocked` → initial; retry may use S4 after
  D5 returns `empty`.
- A WPM2 startup-recovered `ambiguous/confirmation_timeout` attempt carrying
  S1.f `crash_recovery` → NOT initial; possibly submitted, permanently D2-only
  until confirmation. Any legacy/other startup-recovered `interrupted` history
  is likewise NOT initial and never gains S4 eligibility.
- Any `ambiguous/confirmation_timeout` attempt, any confirmed attempt, or any
  attempt carrying a successful-submit anchor → NOT initial; it follows S1/WPM1.
- `failed` attempts are NOT initial. Under the S1.f submit-authority exception
  split, post-acceptance exceptions can no longer produce them: FAILED is
  reachable only from errors proven to occur before any possible acceptance
  (explicit backend rejection / the closed proven-never-submitted results);
  uncertain-acceptance submit exceptions settle anchor-less ambiguous instead.
  Remaining `failed` rows retain existing terminal handling and are never
  silently retried by S4.
- Other `interrupted` reasons retain HEAD behavior and do not gain S4 eligibility;
  only the two proven-never-submitted deferred reasons above can re-enter initial.
- Any other overlapping attempt — including open/outcome-null, crash-recovered,
  ambiguous, confirmed, failed, unresolved, anchored, or interrupted — blocks
  S4 initial eligibility for the ENTIRE candidate batch. Exact-set equality is
  neither required nor sufficient; overlap is the authority. Thus regrouping
  `[m1,m2] → [m1]` and `[m1] → [m1,m2]` cannot repaste `m1`.

**Global concurrency and DELIVERING authority**: the existing per-terminal
delivery lock is the process-local injection lease, but it is never sufficient
alone. EVERY path that can open a DELIVERING attempt for a terminal — S4 busy
initial, ordinary ready initial, and WPM1 corrective reinjection — uses the SAME
`begin_delivery_attempt_if_no_other_delivering(...)` primitive. Behavior-specific
admission (S4 overlap/D5, ordinary ready/dialog gates, corrective S1 boundary)
is evaluated outside and before this common opener; none may call legacy
`begin_delivery_attempt` directly.

While holding the delivery lock and BEFORE any attempt/message mutation, the
common seam calls `list_delivering_attempts_for_terminal(terminal_id)`, which
joins DELIVERING inbox rows to attempt members. Any row blocks opening.

**Behavior-specific durable admission proof**: preflight produces one tagged
`AdmissionProof` and passes it to the common opener. The proof records candidate
IDs plus a canonical fingerprint of its named DB read-set; it is diagnostic/
comparison input, not authority by itself. Inside the SAME `BEGIN IMMEDIATE`,
BEFORE candidate CAS or attempt insertion, the opener authoritatively re-runs
the corresponding read-set and admission predicate. For EVERY tag, both the
canonical read-set fingerprint and the predicate must still match; even a new
proven-never-submitted deferred row makes this invocation stale (a later wake may
preflight again and admit it lawfully):

- `s4_initial`: re-query ALL attempt-member histories overlapping candidate IDs
  and re-apply the closed S4 rule (none or only proven-never-submitted deferred
  reasons). Any new/open/ambiguous/confirmed/failed/interrupted overlap is stale.
- `corrective`: re-read the exact `prior_attempt_uuid` named by gate evidence;
  require its exact candidate member set, `ambiguous/confirmation_timeout`,
  persisted anchor + `boundary_exhausted_at`/authorizing `boundary_snapshot`, and
  NO attempt whose `prior_attempt_uuid` points to that source. The proof also
  fingerprints that source's exact `payload_hash`, `started_at` scan window,
  and `TranscriptAuthorityIdentity` (binding row id, session id, path, inode,
  resolution kind). It carries `baseline_size` from the source attempt's durable
  VERSIONED `evidence.last_observed_ref.size`, separately from identity and NOT
  as an exact-match field against live file size; an unversioned or absent
  cursor is no baseline — `stale_admission`, out-of-transaction migration first.
  Inside the transaction, after DB source/no-successor revalidation and BEFORE
  candidate CAS, re-resolve/fingerprint that authority and run ONE non-polling,
  continuity-aware D2 lookup for the source hash/window. Missing/changed source,
  successor, binding/reference rotation, unresolved continuity, or D2 hit is
  `stale_admission`; the service gate confirms the hit or defer-retries unresolved
  authority on the next wake. The opener never settles from inside this branch.
- `ordinary`: re-fingerprint all overlapping prior attempts (UUID, exact members,
  outcome, reason, payload hash, prior UUID, and evidence hash) plus the current
  durable transcript-binding identity/read reference used by preflight. Re-run a
  single non-polling continuity-aware D2 lookup for applicable prior payload hashes
  against that authority. New/changed history, binding/reference change,
  unresolved continuity, or a hit requiring D2 settlement makes admission stale;
  it returns to the service gate rather than pasting.

**Bounded transcript work under `BEGIN IMMEDIATE`** (corrective + ordinary):

- Exact identity comparison covers ONLY binding row id/session/path/inode/
  resolution kind plus the applicable source payload hash and `started_at`
  window. `baseline_size` is read from the canonical durable VERSIONED
  `evidence.last_observed_ref` cursor and stored separately in the proof. The
  durable nested baseline itself must still equal the proof baseline recomputed
  in-transaction; a concurrent w5 advance makes this invocation
  `stale_admission` and a later wake builds a fresh proof. Live file
  `current_size` is expected to grow and is NEVER compared for equality with the
  baseline.
- `MAX_IN_TXN_TRANSCRIPT_DELTA_BYTES = 1_048_576` (1 MiB). A proof without a
  valid continuity baseline cannot trigger an in-transaction full scan; it is
  immediately `stale_admission` for out-of-transaction refresh.
- Open/fstat the exact bound path, require the same identity and
  `current_size >= baseline_size`, then inspect `[baseline_size,current_size)`:
  `seek(baseline_size)` and read at most `min(delta, cap + 1)` bytes. When
  `delta <= cap`, parse that exact complete interval. Same-identity size growth
  is the input to D2, not `stale_admission`. When `delta > cap`, the `cap + 1`
  read establishes overflow and returns `stale_admission` without treating the
  partial interval as absence. Truncation, replacement, malformed bytes/JSON, or
  binding change also returns `stale_admission`. Calling `Path.read_bytes()` or
  reading prefix bytes before the continuity offset while the write transaction
  is open is forbidden.
- Group applicable hashes by identical binding/path/inode/baseline. Read and parse
  each suffix ONCE, then compare the closed set of payload hashes/windows in that
  one pass; never rescan the suffix per prior attempt. Native-turn priority and
  queued-command semantics remain S1.a.
- No polling/sleep occurs in the transaction. The 1 MiB cap bounds file I/O/parser
  work independently of total transcript size; contention still uses the frozen
  3 attempts × 1s busy policy.

Any read-set/predicate mismatch returns `stale_admission`. Non-durable status,
dialog, and D5 checks remain behavior-specific preflight gates, but can only
narrow admission; they never substitute for the transactional durable recheck.

**Closed opener result protocol**: `begin_delivery_attempt_if_no_other_delivering`
returns a tagged `AttemptOpenResult`, never a bare UUID or status string:

- `opened(attempt_uuid)` — the ONLY result that allows backend send.
- `delivering_conflict` — outer preflight or in-transaction exact-self check saw
  another DELIVERING attempt for the terminal.
- `busy_aborted` — the bounded immediate transaction exhausted contention at
  BEGIN, any write/flush, or COMMIT; the literal `_run_wpm1_immediate`
  `"busy_aborted"` is mapped to this tag and can never be interpreted as a UUID.
- `stale_candidate` — candidate IDs/receiver/PENDING CAS no longer match.
- `stale_admission` — behavior-specific durable admission read-set/predicate
  changed after preflight (including a peer attempt settled back to PENDING).

Every non-open tag returns from the service path BEFORE backend send, leaves the
entire candidate set PENDING through no-write or rollback, creates no durable
attempt/member rows, and never falls into generic FAILED settlement.

**Atomic candidate CAS/open**: the primitive uses the existing bounded
`_run_wpm1_immediate` / `BEGIN IMMEDIATE` spine and, inside that ONE transaction:

1. Re-run the no-other-DELIVERING query and the tagged durable admission proof
   above; any mismatch exits before row mutation.
2. Load the sorted unique candidate IDs and require the exact set to exist with
   `receiver_id == terminal_id` and `status == PENDING`.
3. Conditionally UPDATE exactly those rows with predicates `(id IN candidate) ∧
   receiver_id == terminal_id ∧ status == PENDING` to DELIVERING. The changed
   rowcount MUST equal candidate cardinality. Missing/wrong-receiver/non-PENDING
   or rowcount mismatch → rollback + `stale_candidate`.
4. Only after the successful CAS, insert the attempt and exact member rows, then
   flush. Before commit, query the terminal's DELIVERING attempts; the result must
   be exactly `{just_created_attempt_uuid}` with exactly the candidate member set.
   An extra attempt → rollback + `delivering_conflict`; candidate/self mismatch →
   rollback + `stale_candidate`.

Therefore the just-created attempt is the sole DELIVERING exception only AFTER
the atomic open commits. No terminal arm can be resurrected from DELIVERED or
DELIVERY_FAILED because those rows fail the PENDING predicate and rowcount gate.
`begin_delivery_attempt` may remain as a lower-level/test compatibility symbol,
but no production inbox writer routes through it after WPM2.

Distinct durable ambiguous batches follow the proof-safe release policy in
S1.d: classifier-positive permanent D2-only member sets are skipped without
paste, and later DISJOINT batches may proceed. Ordinary non-protected ambiguous
batches retain their S1/WPM1 gate behavior. No batch is collapsed into another payload or cap;
this gives one concurrent paste maximum and one proof chain per member set.

**Eager flag interaction**: S4 SUPERSEDES `EAGER_INBOX_DELIVERY` for this exact
Claude initial-delivery arm. A Claude PROCESSING receiver with D5 `empty` is
eligible whether the environment flag is true or false. The flag continues to
govern its existing generic/provider eager path and every non-Claude path; it
does not disable or broaden S4.

Expected behavior: first injection within one scheduler pass (≤ ~30s
reconciliation period) regardless of receiver busyness; confirmation typically
arrives late via `transcript_queued_command` at the receiver's next turn
boundary; attempt chain length stays 1.

Evidence bar:
- Busy receiver, message sent → injection within one pass, exactly 1 injection,
  confirm via queued_command on flush, DELIVERED, chain length 1.
- Composer `nonempty`/`unresolved` → hold (unchanged D5 behavior), no injection.
  `unresolved` has two separate mandatory fixtures: capture failure and
  parser ambiguity; both reach the same fail-closed no-paste arm.
- **Real D5 substrate**: commit byte-exact captured busy-Claude PROCESSING
  artifacts at `test/fixtures/claude_busy_processing/{empty,nonempty,
  parser_ambiguous}.txt` plus `capture_failure.json`. Provider parser tests
  consume those captures, not synthetic strings. Named end-to-end test
  `test_wpm2_busy_claude_real_empty_frame_reaches_initial_paste` feeds the real
  empty PROCESSING frame through `ClaudeCodeProvider.read_composer_draft_state`
  and reaches the backend paste without mocking D5. Named fail-closed tests
  `test_wpm2_busy_claude_real_nonempty_frame_holds`,
  `test_wpm2_busy_claude_parser_ambiguity_is_unresolved`, and
  `test_wpm2_busy_claude_capture_failure_is_unresolved` never paste.
- Initial-history matrix: no-attempt and deferred-never-pasted are S4-eligible;
  crash-recovered ambiguous, startup-recovered interrupted, ordinary ambiguous,
  confirmed/anchored, generic failed, and other interrupted histories are not.
  Mutation treating crash-recovered/interrupted or failed as initial must die.
- Overlap regrouping: named tests
  `test_wpm2_superset_attempt_blocks_subset_initial_repaste` (`[m1,m2]→[m1]`)
  and `test_wpm2_subset_attempt_blocks_superset_initial_repaste`
  (`[m1]→[m1,m2]`) cover every non-proven-never-submitted outcome class. Mutants
  restoring exact-member-set-only lookup or ignoring overlap must die.
- DELIVERING atomicity: named test
  `test_wpm2_s4_preflight_and_post_open_allow_only_created_attempt` asserts the
  preflight occurs under the held delivery lock before mutation and the committed
  post-open set is exact-self. A coordinated second-connection test attempts a
  conflicting open. Mutations removing the DB QUERY (outer preflight or atomic
  helper query), retaining only the process lock, or omitting the post-open query
  must die. Any classifier-positive protected head + later disjoint candidate
  remains admissible because the head is PENDING, not DELIVERING.
- Opener tags/service exits (codex r7 B3):
  `test_wpm2_opener_outer_preflight_conflict_never_sends`,
  `test_wpm2_opener_busy_at_begin_write_or_commit_never_sends` (three injection
  points), and `test_wpm2_opener_post_open_invariant_failure_never_sends` assert
  the exact non-open tag, all candidate rows PENDING, no attempt/members, zero
  backend calls, and no generic FAILED transition. A mutant returning the bare
  `_run_wpm1_immediate` string and treating it as `attempt_uuid` must die.
- Candidate CAS races (codex r7 B4): two-connection tests
  `test_wpm2_d2_confirm_vs_open_both_commit_orders` and
  `test_wpm2_terminal_settlement_vs_open_both_commit_orders` coordinate the
  common opener against D2 DELIVERED settlement and terminal
  DELIVERED/DELIVERY_FAILED settlement. Settlement-first → opener
  `stale_candidate`, zero send, terminal status preserved. Open-first → opener is
  sole DELIVERING winner and settlement CAS reports stale; at most its one send
  occurs and no terminal status is resurrected. Mutations removing the PENDING
  predicate or accepting changed-rowcount != exact candidate cardinality must
  produce resurrection/dual ownership and die.
- Admission-history races (codex r8 B2): parameterized across `s4_initial`,
  `corrective`, and `ordinary` proofs,
  `test_wpm2_preflight_vs_peer_ambiguous_settle_both_commit_orders` and
  `test_wpm2_preflight_vs_peer_deferred_settle_both_commit_orders` reproduce the
  live sequence where a peer opens then settles back to PENDING between preflight
  and opener. Peer-first → caller returns `stale_admission`, zero send; caller
  first → caller is the sole opened/send winner and the peer's transactional
  revalidation/conflict path cannot create a second paste. Final history never
  contains two admitted attempts for the same submission opportunity. Mutations
  accepting only the pre-transaction `AdmissionProof` fingerprint, skipping the
  in-transaction overlap/source/transcript requery, must reproduce duplicate
  paste and die.
- Corrective transcript-authority races (codex r9 B1):
  `test_wpm2_corrective_d2_hit_between_preflight_and_open_is_stale_admission`
  appends a real queued-command hit after proof creation; the in-transaction
  lookup returns `stale_admission`, zero send, and the next service wake confirms
  DELIVERED. A read/parser spy asserts the appended byte range was ACTUALLY read
  and the queued-command record matched; a mutant returning stale solely because
  `current_size != baseline_size`, before parsing, must die.
  `test_wpm2_corrective_binding_rotation_between_preflight_and_open_is_stale_admission`
  rotates the binding/path/inode and proves zero send plus defer/re-resolve next
  wake. Mutations deleting the corrective lookup or trusting only its preflight
  hit/miss/reference must paste and die.
- Bounded in-transaction transcript evidence (codex r9 S2):
  `test_wpm2_large_transcript_multi_hash_admission_reads_one_bounded_suffix`
  creates a large sparse/real transcript prefix, a continuity baseline at its
  tail, a <=1 MiB appended suffix, and multiple prior hashes sharing that
  authority. Read spies assert zero prefix/full-file reads, one suffix read,
  bytes read <= `cap + 1`, and one parsed pass for all hashes; measured opener
  transaction duration stays below one 1s busy-attempt envelope. A >1 MiB delta
  case returns `stale_admission` without send. Whole-file, per-hash-rescan, and
  cap-removal mutants must die.
- Cap-overflow recovery (codex r10 S1):
  `test_wpm2_overflow_then_service_refresh_finds_hit_without_open` places a valid
  queued-command hit beyond the first 1 MiB delta. The opener stales once; the
  next OUT-OF-TRANSACTION service D2 refresh uses the SAME identity/continuity
  epoch, scans the complete growth, finds the hit, settles DELIVERED, and never
  calls the opener/backend. `test_wpm2_overflow_absent_refresh_advances_baseline_then_opens`
  supplies >1 MiB valid absent JSONL; the next service D2 scans it outside the
  write transaction and atomically advances the attempt's canonical
  `last_observed_ref` to current size through w5. The test closes the service and
  DB handles, constructs a fresh service/process fixture, reloads the attempt from
  SQLite, and asserts the later AdmissionProof uses that durable advanced cursor
  (zero/new bounded delta) and may open once if all other admission facts remain
  true. Parameterized ambiguous and ordinary-`interrupted` prior rows take this
  same restart path. `test_wpm2_overflow_cursor_crash_before_and_after_commit`
  proves before-commit rescan and after-commit restart reuse. Reusing the old
  cursor, treating cap overflow as permanent stale, mutating only the caller's
  evidence dict, or directly UPDATEing evidence outside w5 must starve/violate
  these continuations and die.
- Mixed-writer closure (codex r6 B3): two-connection races
  `test_wpm2_s4_vs_ordinary_initial_share_atomic_delivering_opener` and
  `test_wpm2_s4_vs_corrective_share_atomic_delivering_opener` coordinate both
  admission paths against one terminal; exactly one DELIVERING attempt commits
  and the loser stays PENDING/no-paste. Mutations routing the ordinary or
  corrective peer through legacy `begin_delivery_attempt` must reproduce the
  live-probe `[legacy,s4]` dual-DELIVERING state and die.
- `EAGER_INBOX_DELIVERY=false` still permits the exact Claude S4 initial arm;
  non-Claude behavior remains flag-gated. Mutants making the flag disable S4 or
  making S4 supersede the flag globally must die.
- Non-claude receivers → HEAD gating byte-unchanged.
- Corrective path unaffected: a reinjection-eligible batch still requires the
  full S1.b cycle proof (wiring mutant: S4 gate applied to step-6 must die).

## S3 — WP2S3 C1: seed UUID capture reads stderr

**Impl pinned (single)**: the seed invocation runs with `stderr=subprocess.STDOUT`
(merged at process execution — no string concatenation of separately captured
streams). Parse strictness otherwise unchanged; candidate set is deduplicated by
VALUE: multiple copies of one UUID = one candidate; two DISTINCT UUIDs =
fail-closed with the existing `seed_uuid_unparseable`-class error, never a guess.

Evidence bar:
- stderr-only fixture (real drain shape: `c1/prod-argv-stderr.txt` 3312B, one
  UUID; stdout 8B) → captured.
- stdout-only legacy fixture → captured (no regression).
- Interleaved garbage + duplicate same-UUID across streams → one candidate,
  captured; two distinct UUIDs → fail-closed.
- No-trailing-newline fixtures on both streams (merge cannot fuse tokens across
  a missing boundary).
- On activation, drain re-runs WP2S3 C1 end-to-end (plain codex create); memory
  `codex-plain-spawn-broken` retires only on that PASS.

## Out of scope

- WPM1 cap algebra, D5–D9 arm behavior, outcome/status vocabulary (D7), evidence
  fence — unchanged; S1 touches only step-5/step-6 eligibility + D2 oracle
  shapes as pinned above.
- Historical backfill of pre-WPM2 `delivery_failed` rows (residual, own slice).
- Non-claude provider confirmation semantics.
- MSGTRACE RESIDUAL-2 binding carve-out (S1.a operates within resolved bindings
  only).
- FX7 `_waiting_inbox_episodes` + WPM1 D4 suppression internals (byte-untouched;
  S2 seam = assignment stalled-callback alarm hygiene only).
- Upstream v2.3.0 merge content (`7148c58`, inner) — same activation train, not
  gated here beyond the standard full suite.

## Expedited subset (committed ahead under user priority, 2026-07-13)

Commit `f309165` contains the S1.a oracle widen plus the S2 fired-latch /
immutable-D4-bound subset after its expedited build and review (live token burn;
both pieces individually dual-lane-ratified by r2/r3 reviews). It is an ancestor
of the WPM2 build baseline. The full build extends those committed bytes; this
blueprint remains the law for the complete feature, and the diff gate reviews
the combined baseline-relative WPM2 diff normally.

## Gate plan

Dual-lane standard: codex empirical MAIN (terminal holds WPM1 r1–r3 + WPM2 r1
context), grok structural double-check. Freeze on dual zero-decision YES → build
(codex_dev fork_from=codex) → diff gate. Evidence-only rounds hash-pinned. Full
suite + focused: `test_wpm1_delivery.py`, `test_stalled_callback_watchdog.py`,
codex seed/provider unit files, new WPM2 evidence file.
