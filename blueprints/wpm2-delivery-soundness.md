# WPM2 — delivery soundness for busy receivers + alarm hygiene + seed stderr parse

Status: DRAFT r2 (2026-07-13). Micro-WP, three independent slices sharing one gate
train. Builds directly on WPM1 (`8afb758`, FROZEN r9 law) and WP2S3 (`7651dc1`).

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
authority itself.

### S1.b — loss-boundary predicate (closes codex B2, grok B1; replaces r1's deferred pin)

A `boundary_exhausted_at` (proven boundary loss) may be written for a
claude_code attempt ONLY when ALL hold:

1. Gate set identical to WPM1 D1.4: receiver status ∈ {IDLE, COMPLETED} AND
   D5 composer tri-state == `empty` (full D1.4/D5 inheritance — mandatory, not
   reopened).
2. **Post-injection turn-cycle evidence** (excludes the incident's false-COMPLETED
   class): one atomic boundary observation (S1.c) shows a non-ready
   (PROCESSING-class) observation followed by a ready observation, BOTH sequenced
   strictly after this attempt's injection completion (`last_at`). A level sample
   of COMPLETED after injection — the exact incident state — is NOT boundary
   evidence. Generation comparisons over `pre_input_gen`/`pre_status_gen`/
   `settled_status_gen` are NOT sufficient substitutes (empirically falsified:
   tuples above advanced across three false losses).
3. D2 lookup with the S1.a oracle runs FIRST at that observed boundary and
   returns absent. A hit (either confirmed kind) settles confirmed/DELIVERED and
   the batch exits the corrective path (WPM1 D2 unchanged).

Wall-clock expiry alone NEVER writes exhaustion and NEVER authorizes injection.
Non-claude providers: S1 does not apply; their existing WPM1 semantics are
untouched.

### S1.c — atomic boundary observation + wake law (closes codex B3)

- One **boundary-observation snapshot object** sampled under a single
  lock/version: (status, status_gen, input_gen, monotonic observation seq).
  Mixed reads of `get_status()` and `get_status_gen()` from different detections
  are forbidden on this path; the snapshot (or its relevant fields) is persisted
  into the attempt evidence when it authorizes a loss.
- **Wake law**: loss-boundary evaluation is wake-driven as today (published
  status events). Because a same-status ready redraw does not publish
  (`status_monitor.py:257-269`), and queued deliveries flush at receiver turn
  boundaries invisible to the bus, D2 RE-CONFIRMATION (S1.a lookup only, never
  loss-writing) additionally runs on the existing periodic wake/poll spine that
  drives WPM1 D1 today — every wake re-runs D2 before any other arm (D1.2
  ordering unchanged). Liveness clock: confirmation advances on transcript
  flush; loss advances only on observed turn cycles. A receiver that never
  exhibits a post-injection turn cycle keeps the message pending — lawful;
  longevity is covered by **WPM1 D1.3 stalled notice (D8 atomic, once per
  batch) + D9 exempt retention** (NOT the S2 assignment watchdog, which is a
  separate alarm class on worker assignment episodes).

### S1.d — attempt lifecycle: option A, no new vocabulary (closes grok B2)

The confirm window may still settle an attempt `ambiguous`/`confirmation_timeout`
on wall-clock exactly as at HEAD. S1 narrows ONLY D1 step-5 exhaustion
eligibility (S1.b) and step-6 reinject eligibility (exclusively the step-6 path,
now gated on S1.b). No new outcome values, no new message statuses, no new
columns beyond attempt evidence JSON keys (D7 honored). `recover_stale_deliveries`,
wake selection, and D9 gated-PENDING detection semantics are unchanged.

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
   explicit clear. A caller SEND_MESSAGE arriving while an episode is active and
   unanswered JOINS the episode (may update `inbound_wall_at` for grace-timing)
   but does NOT reset `fired` and does NOT create a fresh alarm-eligible
   episode. A new episode (and thus new alarm eligibility) begins only after the
   prior episode ended (callback/clear).
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
- **Wiring mutant (named)**: re-enable `fired` reset on caller SEND_MESSAGE
  (the HEAD bug) — the PRIMARY test must fail (kill).

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

## Gate plan

Dual-lane standard: codex empirical MAIN (terminal holds WPM1 r1–r3 + WPM2 r1
context), grok structural double-check. Freeze on dual zero-decision YES → build
(codex_dev fork_from=codex) → diff gate. Evidence-only rounds hash-pinned. Full
suite + focused: `test_wpm1_delivery.py`, `test_stalled_callback_watchdog.py`,
codex seed/provider unit files, new WPM2 evidence file.
