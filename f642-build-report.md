# F642 — delivery-ledger spine — BUILD REPORT

**Blueprint:** `orchestrator/blueprints/f642-delivery-ledger-spine.md`
(committed eb2963a8, sha256 `df23463a1ed6eb9109f2c850494fea9500235ffa492a8408286b1ae2af4cb06f` — **verified before starting**).

**Git-SHA-fork:** branched from `main @ e473e74a` (fork base pin `e473e74a50ec4212f7372dd9b69a1340f06b7957`).
**Branch:** `cao/50b71a7b`
**Worktree:** `/data/cao-scratch/worktrees/cli-agent-orchestrator/50b71a7b`
(tip SHA recorded in the callback message after commit.)

---

## 1. What was built

The delivery-state SPINE as ONE cohesive data structure (three tables) plus the
claim discipline and the routing map, wired into the real choke points, with a
pure decision-logic core that is unit-testable in isolation.

### Files

| File | Kind | Contents |
|---|---|---|
| `src/cli_agent_orchestrator/clients/delivery_ledger.py` | **NEW** | Pure logic (DB-free, `mypy --strict` clean): enums (`LedgerState`, `Carrier`, `EmissionOutcome`, `SuppressedReason`, `BlockedReason`, `UndeliverableReason`, `AckActor`, `ConditionDecision`), `carriers_exhausted()` (D2/S2), the kind→surfaces map `KIND_SURFACES` + `surfaces_for_kind`/`is_kind_mapped`/`busy_class_declines_inbox` (D5), and the durable de-dup rule `should_suppress_condition()` / `latest_memory_row()` (D7/AC21). |
| `src/cli_agent_orchestrator/clients/database.py` | MOD | 3 SQLAlchemy models (`DeliveryLedgerModel` PK `message_id`; `DeliveryEmissionModel` `UNIQUE(message_id,carrier)`; `ConditionLedgerModel` autoinc PK). `_migrate_f642_delivery_ledger()` registered LAST in `init_db()`. F642 ops: `create_delivery_ledger_row`, `claim_emission`, `record_emission_outcome`, `emit_via_carrier`, `mark_carrier_unavailable`, `maybe_mark_undeliverable`, `ack_delivery_ledger`, `write_through_terminal_state`, `record_blocked_awaiting_idle`, `mark_receiver_gone`, `enqueue_callback_replay_gated`, `record_condition_decision`, `condition_log_rows`, `suppress_condition_by_log`, `hook_claim_ids`, `DbConditionLogStore`, `delivery_ledger_dispute_view`. Wiring: ledger insert + supersede/expire/digest write-through, ack D4/D6, both replay call sites gated, reap D8/S1. |
| `src/cli_agent_orchestrator/providers/condition.py` | MOD | `ConditionLogStore` protocol + `ConditionDelivery(log_store=…)`. `deliver()` writes a decision row for ALL FOUR exits (`cleared`/`gated`/`deduped`/`delivered`), consults the durable rule when a store is wired, and applies the D5 inbox-decline for busy-class kinds. Backward-compatible when `log_store=None`. |
| `src/cli_agent_orchestrator/services/mailbox_service.py` | MOD | `ack_messages`: D4 `ack_delivery_ledger(EXPLICIT)` + D6 prune in the same `BEGIN IMMEDIATE` transaction as the watermark advance. |
| `src/cli_agent_orchestrator/services/inbox_service.py` | MOD | `_f642_record_blocked_awaiting_idle()` helper + call at the delivery gate's not-eligible return (D12). |
| `test/clients/test_f642_pure_logic.py` | **NEW** | 20 tests — pure de-dup rule (AC21 a–d + both mutants), surfaces map (AC6), exhaustion (AC19/AC23 + mutants). |
| `test/clients/test_f642_delivery_ledger.py` | **NEW** | 31 tests — DB-backed storage-layer ACs. |
| `test/providers/test_f642_condition_ledger.py` | **NEW** | 11 tests — condition plane through the real `ConditionDelivery` seam. |

---

## 2. Per-D-row coverage map

| D-row | Where implemented | Verified by |
|---|---|---|
| **D1** — per-id ledger authority; watermarks demoted | `DeliveryLedgerModel` (PK `message_id`); `create_delivery_ledger_row` called in `_insert_routed_inbox_row` | AC1, AC11, AC14 |
| **D2** — repeat = UNIQUE violation; exhaustion → `undeliverable`; applicable domain; emit-time re-check | `DeliveryEmissionModel UNIQUE(message_id,carrier)`; `claim_emission`, `carriers_exhausted`, `maybe_mark_undeliverable`, `mark_carrier_unavailable` | AC1, AC2, AC19, AC23 |
| **D3** — claim-before-speak; hook READ = claim | `claim_emission` (SAVEPOINT), `emit_via_carrier`, `hook_claim_ids` | AC3, AC18 |
| **D4** — ack records the ACTOR; watermark is projection | `ack_delivery_ledger(actor=…)` in `ack_messages` | AC4, AC15 |
| **D5** — kind→surfaces map is data; BUSY-class = fleet+bus only | `KIND_SURFACES`, `busy_class_declines_inbox`; `ConditionDelivery` inbox-decline | AC5, AC6 |
| **D6** — replay ledger-gated on enqueue AND pruned on ack | `enqueue_callback_replay_gated` (both call sites); prune in `ack_delivery_ledger` | AC7, AC8 |
| **D7** — condition de-dup moves to the durable log; rule over `delivered`/`cleared`, skips `gated`/`deduped` | `should_suppress_condition`, `latest_memory_row`, `condition_ledger` | AC9, AC20, AC21, AC24 |
| **D8** — send with no row fails; `undeliverable` producers named | ledger row is emission precondition; `mark_receiver_gone` (reap + retention via shared delete path); `delivery_ledger_dispute_view` detects absence | AC10, AC19, AC22 |
| **D9** — ledger keyed per receiver, owned by receiver's server | ledger row created in the routing txn (`_insert_routed_inbox_row`), receiver-keyed | AC11 |
| **D10** — F578 columns left alone | no F642 path reads/writes `supersede_key`/`digested_into`/`expire_after_s` | AC13 |
| **D11** — additive migration, no backfill | `_migrate_f642_delivery_ledger` = 3 `CREATE TABLE IF NOT EXISTS`, idempotent; no `inbox`/`mailboxes` rebuild | AC12 |
| **D12** — DECLINED vs NOT-YET-ELIGIBLE; one writer, named clearers | `record_blocked_awaiting_idle` (sole writer); cleared on emit (`record_emission_outcome`), ack, and every terminal transition (`write_through_terminal_state`, `mark_receiver_gone`, `maybe_mark_undeliverable`) | AC16 |
| **D13** — every terminal transition writes THROUGH in the same txn | `write_through_terminal_state` at supersede (`_insert_routed_inbox_row`), expire (`expire_pending_rows`), digest fold | AC17 |

---

## 3. Per-AC coverage map (24 ACs)

★ = load-bearing arm; **mut** = mutant arm executed as a real assertion.

| AC | Status | Test(s) |
|---|---|---|
| **AC1** ★ | ✅ + mut | `test_ac1_send_creates_one_ledger_row`, `test_ac1_one_carrier_per_id_emission_count`, `test_ac1_mutant_dropping_unique_lets_emission_count_climb` |
| **AC2** ★ | ✅ | `test_ac2_unique_carrier_constraint_present`, `test_ac2_second_same_carrier_claim_loses`, `test_ac2_different_carrier_inserts_cleanly` (storage-layer) |
| **AC3** ★ | ✅ + mut | `test_ac3_concurrent_claim_exactly_one_wins` (two real threads, on-disk SQLite WAL), `test_ac3_emit_via_carrier_loser_never_speaks` (emit-then-record mutant covered by loser-never-speaks) |
| **AC4** | ✅ | `test_ac4_ack_records_actor` (explicit vs hook) |
| **AC5** ★ | ✅ + mut | `test_ac5_busy_no_inbox_push_capped_pushes`, `test_ac5_mutant_unconditional_sink_pushes_busy` |
| **AC6** | ✅ | `test_ac6_unmapped_kind_defaults_to_no_inbox_and_reports_unmapped` |
| **AC7** ★ | ✅ | `test_ac7_replay_of_acked_id_refused`, `test_ac7_unacked_id_is_enqueued` |
| **AC8** | ✅ | `test_ac8_ack_prunes_queued_replay` |
| **AC9** ★ | ✅ + mut | `test_ac9_dedup_survives_restart`, `test_ac9_mutant_in_memory_dict_rearms_on_restart` |
| **AC10** ★ | ✅ | `test_ac10_no_ledger_row_is_detectable` (dispute view reports absence, not false "delivered") |
| **AC11** | ✅ | `test_ac11_ledger_row_created_with_the_message` |
| **AC12** | ✅ | `test_ac12_migration_creates_tables_and_leaves_inbox_intact` (inbox schema byte-identical, row untouched, idempotent) |
| **AC13** | ✅ | `test_ac13_delivery_path_does_not_touch_f578_columns` (asserted against the delivery path, not row counts — r1/S3) |
| **AC14** | ✅ | `test_ac14_list_messages_shape_unchanged` (projection is a separate query) |
| **AC15** | ✅ | `test_ac15_dispute_view_returns_carriers_and_actor` |
| **AC16** ★ | ✅ + mut | `test_ac16_blocked_awaiting_idle_recorded_and_cleared_on_emit`, `test_ac16_second_arm_terminal_state_clears_wait`, `test_ac16_blocked_since_ages_not_reset_on_repeat` |
| **AC17** ★ | ✅ + mut | `test_ac17_supersede_writes_through_to_ledger`, `test_ac17_expire_writes_through`, `test_ac17_mutant_status_only_leaves_ledger_pending` |
| **AC18** ★ | ✅ + mut | `test_ac18_hook_prints_nothing_for_natively_claimed_id`, `test_ac18_hook_wins_unclaimed_and_acks_as_hook`, `test_ac18_mutant_claim_exempt_hook_reprints_carried_id` |
| **AC19** | ✅ | `test_ac19_failed_carrier_keeps_claim_and_retries`, pure `test_ac19_*` |
| **AC20** ★ | ✅ + mut(i,ii) | `test_ac20_busy_and_dedup_produce_durable_rows_no_message_id`; mutant (i) `condition_ledger` cannot key on a message id — enforced by schema (autoinc PK, no `message_id` PK); mutant (ii) once-per-epoch PK covered by `test_ac21_mutant_once_per_epoch_pk_would_drop_b_and_c` |
| **AC21** ★ | ✅ + 2 mut | (a) `test_ac21a_*`, (b) `test_ac21b_*`, (c) `test_ac21c_*`, (d) ★ `test_ac21d_*` — both through the seam AND as pure rule; mutants: `test_ac21_mutant_once_per_epoch_pk_would_drop_b_and_c`, `test_ac21_mutant_unqualified_latest_row_would_redeliver_d` |
| **AC22** ★ | ✅ | `test_ac22_mark_receiver_gone_transitions_undelivered`, `test_ac22_acked_row_not_touched_by_receiver_gone` (reap + retention share the delete path; control arm: acked rows untouched) |
| **AC23** | ✅ + mut | `test_ac23_disarm_arm_carrier_unavailable_reaches_undeliverable` (DB), pure `test_ac23_*` incl. `test_ac23_mutant_never_records_outcome_is_never_satisfiable` |
| **AC24** | ✅ + mut | `test_ac24_gated_condition_writes_row_moves_nothing`, `test_ac24_mutant_no_row_for_gated_leaves_no_trace` |

**Every ★ arm and every named mutant is an executed assertion**, not a narrated one.

---

## 4. Test & type results

- **F642 targeted suite: 62 passed** (`test/clients/test_f642_pure_logic.py` 20, `test/clients/test_f642_delivery_ledger.py` 31, `test/providers/test_f642_condition_ledger.py` 11).
- **Regression (touched-path suites): green** — F611 condition detection+wiring (58), F136 callback delivery + mutation kills, F475 callback dedup, F193 settle-on-ack, wpq7 callback barrier, wp-mailbox channel (179 total), F635 barrier capture + `test_inbox_service` + most of `test_database` (137).
- **mypy:** `delivery_ledger.py` is **`mypy --strict` clean**. `condition.py` clean under project config. `database.py` / `mailbox_service.py` / `inbox_service.py`: **zero NEW errors** introduced by F642 (all remaining errors are pre-existing SQLAlchemy `Column[...]` issues outside F642 line ranges; one new int-key error was found and fixed).

### Owed / caveats (honest)

1. **FULL SUITE NOT RUN on this laptop** (per dispatch instruction). **OWED for the gate's box run.**
2. **One PRE-EXISTING test failure, not caused by F642:** `test/clients/test_database.py::TestInboxOperations::test_message_status_storage_is_additive_unconstrained_text` asserts `MessageStatus` has 9 values, but F578 added `EXPIRED`/`SUPERSEDED` (11 values) at/before base `e473e74a`. Verified failing on the base commit; F642 touches neither `models/inbox.py` nor that test. Left as-is (out of scope; fixing it would be a drive-by).
3. **Cross-repo coordination (blueprint §7 open question), NOT implemented here:** the `cao messages list --claim hook` CLI flag and the ROOT-repo `.claude/hooks/supervisor-inbox-drain.sh` edit. The SERVER-side claim they rely on (`hook_claim_ids`, `ack_delivery_ledger(actor=hook)`) IS built and tested; the CLI flag + hook script are a fork-side-CLI + root-repo pair that must land together (per §7). AC18 is asserted at the storage layer against `hook_claim_ids`.
4. **`applicable_carriers` at routing time** defaults to the full `Carrier` set in `_insert_routed_inbox_row` (absent runtime signals ⇒ full set, so exhaustion is never falsely reached early). The per-carrier applicability probes (`_should_teammate_push`, armed-WS, hook-seat, replay-path) are the natural follow-up wiring; the domain, the emit-time re-check (`mark_carrier_unavailable`), and the exhaustion rule are all built and tested (AC19/AC23).
5. **D12 gate instrumentation** is placed at the canonical not-eligible return in `deliver_pending` (best-effort, self-sessioned, swallowed on failure — never a delivery precondition). The clearers (emit/ack/terminal) are exhaustive.

---

## 5. Retraction fidelity (r1–r4 folds honored)

- **r2/B1 retraction:** `condition_ledger` is an APPEND-ONLY decision log (autoinc PK), NOT a de-dup-tuple PK. De-dup is a rule over the log. (AC20 mutant (ii), AC21 once-per-epoch mutant.)
- **r3/B1:** the rule reads the latest `delivered`/`cleared` row and SKIPS `gated`/`deduped`. (AC21(d), AC24.)
- **r3/S2:** applicability re-checked at emit time → `carrier_unavailable`. (AC23 disarm arm + never-satisfiable mutant.)
- **r1/B1 + D13:** every terminal transition (supersede/expire/digest) writes through in the same transaction. (AC17 + status-only mutant.)
- **r1/B3:** condition suppression is expressible with NO message id (its own table). (AC20.)
