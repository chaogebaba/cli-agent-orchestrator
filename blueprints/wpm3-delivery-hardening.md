# WPM3 — WPM2 residual hardening (structure-enforce existing law; no new delivery semantics)

Status: DRAFT r2 (2026-07-13). Micro-WP. Law base: WPM2 FROZEN r21 (`8ebd7ca`),
implemented at inner `7b8fc6d`, activated + drained-pass 2026-07-13
(`tmp/orch/drain-wpm2.md`). WPM3 adds **no new delivery semantics**: every item
enforces already-frozen WPM2 law where the r21 implementation satisfies it only
by convention/flag-default rather than by structure, plus two hygiene closures.

Origin: grok design double-check residuals from the WPM2 diff gate
(`tmp/orch/grok-diffgate-wpm2-r1.md` SHOULD S1–S4; `grok-diffgate-wpm2-r2.md`
S6 + N1/N4/N5), routed per ledger as next-slice scope instead of bounced.
All were adjudicated non-blocking at commit time because each drift is
fail-closed or flag-gated today; WPM3 removes the conventions they lean on.

Gate lanes: codex_reviewer empirical MAIN (blueprint gate, then diff gate);
grok_reviewer design double-check (runtime-tier delivery surface → dual-lane).
Build: codex_dev. Build dispatch MUST embed the mutation-ledger artifact
format (see Evidence bar) — three consecutive WPs had builder evidence claims
fail empirical replay.

r1→r2 changelog (folds codex r1 3B/3S — tmp/orch/codex-bpgate-wpm3-r1.md — and
grok r1 2S/1N — grok-bpgate-wpm3-r1.md): H5 WITHDRAWN (codex B1: proposal
reversed frozen S1.f epoch-mismatch law — anchor-less settlement + permanent
`anchor_missing` is the law, not a drift); H3 boundary predicate OR→AND atomic
pair + refusal tests + drop-each-half mutants (codex B2); H4 rewritten to
delete-seam→existing classifier with epoch|None (grok S2), `skip_d2_only`
pass-local semantics with D1.3/D8 notices before skip (grok N1 + codex B3),
and explicit 10-test migration table (codex B3 scratch run: 151/161 with naive
removal); H1 Change replaced with Claude-first branch — EAGER never consulted
for claude_code, S4 refusal terminal for the wake (grok S1, codex concurring);
H6 + typed TerminalNotFound post-submit test/mutant + table-driven all-arms
assertion (codex S1); H2 + independent pre-open restoration mutant (codex S2);
H7 pinned exact map set + omit-one-pop mutant + reset-preserves-new-epoch
(codex S3).

---

## H1 — S4 supersedes EAGER, structurally (grok S1)

**Cite:** `inbox_service.py` normal gate, `eager_eligible` assignment.
**Today:** `eager_eligible = (admission_kind == "s4_initial")` is then
unconditionally reassigned under `EAGER_INBOX_DELIVERY` to
`accepts_input_while_processing`. Correct only while the flag is default-off.
**Law (WPM2 S4):** S4 admission decisions supersede EAGER in both directions —
the flag can neither disable a True S4 eligibility nor grant a busy open that
S4 refused.
**Change (load-bearing shape — Claude-first, EAGER never consulted for claude_code):**
```
# inside status ∉ {IDLE, COMPLETED}:
eager_eligible = False
if provider == claude_code:
    eager_eligible = (admission_kind == "s4_initial")   # EAGER never consulted
elif EAGER_INBOX_DELIVERY and status in {PROCESSING, WAITING_USER_ANSWER}:
    eager_eligible = accepts_input_while_processing(...)
```
For claude_code, S4 is the sole busy-paste authority in BOTH directions: the
flag can neither disable a True S4 eligibility nor grant an open after S4
refused (refusal is terminal for this wake). Non-Claude / eager-native
providers stay on the flag path unchanged.

## H2 — one canonical WPM2 lookup: recovery + pre-open dedup route through `_wpm2_lookup` (grok S2, folds N1)

**Cite:** `inbox_service._recover_wpm2_attempt` (calls
`transcript_lookup(path, hash, started_at, evidence)` with the full evidence
bag as `expected_ref`); pre-open dedup loop in the normal gate (same shape);
dead import of `wpm2_continuity_lookup`.
**Law (WPM2 S1.f):** the containing evidence object is never `expected_ref`;
top-level path/inode/size are not authority when a nested versioned cursor
exists.
**Change:** hoist the lookup (versioned-cursor authority + binding-only
pre-WPM2 carve-out) to ONE module-level function in `message_trace_service`;
`_wpm2_lookup`, `_recover_wpm2_attempt`, and the pre-open dedup loop all call
it; remove the dead import. Recovery and gate must be provably the same
authority (same function object, asserted in tests).

## H3 — corrective in-txn revalidation carries the full admission fingerprint (grok S3)

**Cite:** `_admission_valid(..., "corrective")` — today checks only prior
`ambiguous/confirmation_timeout` + no successor.
**Law (WPM2 S4/S1.f):** corrective admission requires, on the named prior
attempt row: persisted anchor (`injection_completed_seq`), AND
`boundary_exhausted_at` AND its structurally authorizing `boundary_snapshot`
— the frozen ATOMIC pair (snapshot is written only in the same transaction as
exhaustion; either half alone is a malformed row and refuses corrective open;
the DB enforces only one direction today, `database.py` merge allowlist, so
snapshot-only rows are mechanically constructible and must be refused here);
exact member set match; payload/scan-window/TranscriptAuthorityIdentity
fingerprint match; bounded in-txn D2. Snapshot validation is closed-field:
a `boundary_snapshot` authorizes only if it carries the frozen S1.c
observation fields (valid `observation_epoch` str + terminal status + seq)
consistent with the exhaustion it accompanies.
**Change:** encode those predicates inside
`begin_delivery_attempt_if_no_other_delivering`'s corrective arm (or the proof
fingerprint it validates). Missing any durable field → refuse open
(fail-closed), never a permissive corrective.

## H4 — invalid boundary snapshot is never cycle-optional (grok S4)

**Cite:** `legacy_snapshot_seam = True` path forces `protection = "normal"`
and skips S1.b cycle algebra (`if gate_open and not legacy_snapshot_seam`).
**Law (WPM2 S1.b/S1.c):** loss authorization requires the cycle algebra over
valid atomic observations; a non-conforming snapshot seam must not weaken the
predicate.
**Change:** delete `legacy_snapshot_seam` and its cycle bypass; ALWAYS route
protection through the existing classifier:
```
protection = classify_permanently_d2_only(
    newest, snapshot.observation_epoch if valid BoundaryObservation else None)
```
Hard-code NO protection class: with epoch `None` the existing classifier
already yields `busy_initial` → `anchor_missing` (invalid/absent anchor) →
`transient_snapshot_unavailable` (valid anchor) — permanent `anchor_missing`
protection is preserved. Cycle algebra stays gated on `protection == "normal"`
+ valid snapshot (existing `gate_open` path).
**Wake semantics (pass-local, NOT terminal-wide):** a transient/protected head
still runs D1.3/D8 notice processing, then returns `skip_d2_only` — no loss
increment, no exhaustion, and later DISJOINT work in the same pass may still
proceed under the protected-head policy. An implementation returning a
terminal-wide stop violates liveness and must fail the named tests.
**Test migration (empirically measured: naive seam removal fails 10 of 161
WPM1+WPM2 tests — codex r1 scratch run).** The build MUST migrate exactly
these, replacing bare `get_status()` reliance with lawful atomic
observations via ONE shared fixture helper (e.g.
`lawful_boundary_observation(status, *, epoch, seq, anchor=None)` producing a
real `BoundaryObservation` and, where the scenario needs it, the matching
persisted anchor / exhaustion pair / latch sequence):
1. `test_loss_boundary_marks_exhaustion_before_authorizing_successor` — full
   observation cycle per loss + atomic exhaustion/snapshot pair.
2. `test_busy_wake_after_third_injection_cannot_cap` — three lawful
   injection observations + busy cycles that do NOT complete the algebra.
3. `test_third_exhaustion_proof_caps_without_fourth_injection` — three
   proof-ordered exhaustions, each with its snapshot half.
4. `test_threshold_plus_idle_proof_sends_no_stalled_notice` — idle proof as a
   valid observation (epoch + seq), not bare status.
5–6. both cases of `test_stalled_notice_fires_at_30min_idle_or_4h_absolute` —
   episode latch driven by observation sequence, not `get_status()` polls.
7. `test_successor_restart_after_exhaustion_merge_injects_once` — restart
   recovery seeded with the persisted exhaustion pair + anchor.
8. `test_successor_restart_after_paste_return_recovers_and_never_respawns` —
   anchor persisted at mark; recovery reads it, no bypass.
9. `test_interrupted_successor_blocks_duplicate_respawn` — interrupted arm
   with lawful pre-submit observation.
10. `test_cap_barrier_late_payload_confirmation_wins` — cap barrier built
   from proof-ordered exhaustions; late D2 confirm still wins.
Migrations change test SETUP only — every behavioral assertion stays
byte-identical; a migration that weakens an assertion is a gate blocker.

## H5 — WITHDRAWN at r2 (frozen-law conflict; codex r1 B1)

r1 proposed always stamping `injection_completed_seq` across an S4
admission→submit epoch mismatch. Frozen WPM2 S1.f states the OPPOSITE twice
(frozen lines 750–756, 1296–1301): on token mismatch NO valid anchor is
marked; the row settles anchor-less and stays permanently protected via
`anchor_missing`. Codex classifier check: current mismatch settlement →
`anchor_missing` (permanent D2-only); the r1 proposal → `normal` (row could
later exhaust/reinject — reopens the compact false-loss class). The grok S6
residual reading ("drifts from once-marked-durable") loses to frozen law.
Changing this requires a WPM2 law amendment + new payload-specific proof —
OUT of this slice. No H5 work items remain.

## H6 — post-submit ambiguous settle stops the wake in the generic arm (grok N4)

**Cite:** outer generic `Exception` arm — after `proof_safe` returns
`settled`, control falls through to error logging + `sent_count +=
len(batch)`; the confirm-timeout arm `return`s.
**Law (WPM2 S1.d/S1.f):** a post-submit ambiguous settlement ends the wake for
that terminal; no further groups are prepared.
**Change:** `return` after ANY post-submit `proof_safe` call in the outer
arms, regardless of result (`settled` and `settlement_pending_recovery`
alike). This covers the generic `Exception` arm AND the typed
`TerminalNotFoundError` arm (which also calls `proof_safe` post-submit and
currently falls through); `DeliveryDeferredError`/`TerminalInputBlockedError`
already return — pin all four with ONE table-driven assertion so "any outer
arm" is load-bearing, not prose. Multi-group drain resumes on the next wake.
(Codex two-group probe confirmed the current fall-through prepares
`['one','two']`; stopping loses no lawful same-wake continuation — normal
ambiguous work keeps FIFO, protected release re-passes D1.3/D8 later.)

## H7 — epoch/seq map hygiene on terminal free (grok N5)

`clear_terminal` / `reset_buffer` call `_new_epoch_locked` but never `pop` the
per-terminal maps. On terminal FREE only, pop the exact set:
`_observation_epoch`, `_observation_seq`, `_last_non_ready_seq`,
`_last_ready_seq` (status_monitor). `reset_buffer`/rebind keep entries and
continue opening a fresh epoch — reset semantics stay byte-law equivalent.
Pure hygiene; no law text.

## Out of scope

- Any new delivery semantics, thresholds, or evidence keys (WPM2 r21 law is
  closed; H1–H6 cite existing law lines).
- EAGER_INBOX_DELIVERY default flip (stays off).
- D9 retention boundedness, zombie-quarantine automation, upstream P1/P2
  (separate tracks).

## Evidence bar (tests derive from THIS text)

Named roster (deliberately compact — WPM2's 78-name roster was the right bar
for new semantics; hardening needs precision, not volume):

- H1: `test_wpm3_eager_flag_cannot_disable_s4_eligibility`,
  `test_wpm3_eager_flag_cannot_open_busy_paste_s4_refused`,
  `test_wpm3_eager_native_provider_unaffected`
- H2: `test_wpm3_recovery_and_gate_share_lookup_authority` (asserts same
  function object + behavior), `test_wpm3_recovery_rejects_evidence_bag_as_expected_ref`,
  `test_wpm3_preopen_dedup_uses_canonical_lookup`
- H3: `test_wpm3_corrective_refused_without_persisted_anchor`,
  `test_wpm3_corrective_refused_snapshot_only_row`,
  `test_wpm3_corrective_refused_exhaustion_only_row`,
  `test_wpm3_corrective_refused_on_member_set_mismatch`,
  `test_wpm3_corrective_refused_on_fingerprint_mismatch`,
  `test_wpm3_corrective_opens_with_full_fingerprint`
- H4: `test_wpm3_invalid_snapshot_transient_no_loss_increment`,
  `test_wpm3_invalid_snapshot_cannot_exhaust_cap`,
  `test_wpm3_invalid_snapshot_notices_before_skip` (D1.3/D8 run, then
  `skip_d2_only`), `test_wpm3_invalid_snapshot_releases_disjoint_work`
  (later disjoint head in the same pass still proceeds),
  `test_wpm3_no_production_cycle_bypass_flag` (structural: bypass symbol gone)
  — plus the 10 migrated WPM1/WPM2 tests (assertions byte-identical)
- H6: `test_wpm3_post_submit_settled_stops_wake_multi_group`,
  `test_wpm3_post_submit_recovery_stops_wake_multi_group`,
  `test_wpm3_post_submit_terminal_not_found_stops_wake_multi_group`,
  `test_wpm3_outer_arms_post_submit_table` (table-driven: all four arms)
- H7: `test_wpm3_epoch_maps_popped_on_terminal_free` (asserts all four maps),
  `test_wpm3_reset_buffer_keeps_entries_opens_fresh_epoch`

Mutation ledger (REQUIRED, per-mutant artifacts; format goes verbatim into the
build dispatch): for each mutant — applied diff, exact pytest command, failing
exit code + one-line excerpt, post-restore hash proof. Minimum mutant set:
EAGER-reassign restored (H1); recovery reverted to `transcript_lookup`+bag
(H2a); pre-open dedup reverted to direct `transcript_lookup`+bag,
INDEPENDENTLY (H2b); each corrective predicate dropped individually,
including drop-exhaustion-half and drop-snapshot-half separately (H3);
`legacy_snapshot_seam` bypass restored (H4); `return` after settled removed
in the generic arm (H6a); `return` removed in the TerminalNotFound arm,
independently (H6b); one map pop omitted (H7). Every mutant must be killed by
a named test that drives the PRODUCTION path (WPM2 r2 m13 lesson:
classifier-only smoke does not count).

Suite: builder logs full default suite to `tmp/orch/suite-wpm3.log`;
supervisor verifies the log, never re-runs.

## Drain criteria (post-activation, scratch only)

Thin — behavior is flag-gated/fail-closed, live surface small: (1) busy
claude_code receiver delivery still confirms via queued_command (regression
guard on H1/H6 territory, same D1 repro as WPM2 drain); (2) one forced
invalid-snapshot wake → held, zero attempt rows, zero loss increments (H4);
rest suite-covered.

## Gate plan

Blueprint gate: codex_reviewer empirical (may run scratch probes against
`7b8fc6d` tree to verify each "Today" cite is byte-accurate) + grok_reviewer
design double-check. Freeze on dual zero-decision. Then codex_dev build on a
fresh fork base; dual-lane diff gate; commit; ledger row; activation rides the
next natural restart.
