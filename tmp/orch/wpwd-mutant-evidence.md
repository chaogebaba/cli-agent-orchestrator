# WPWD Mutant Evidence

Build commit: `e005c41d3b026dc703e76c0aeb0958fac8732db2`
Authority: `blueprints/wp-watchdog-delegation.md`
Authority SHA-256: `d7caebe8a53c26931337a6e335c487421871e15a6d8940e9536e26e9071d8403`

The fixture selector command was run at the build commit. The new production
path union is 10 tests (`test_wp_watchdog_production_paths.py` plus the two
strengthened watchdog selectors). The pre-existing frozen union collected 67
tests and passed 67 before mutation. Every row below records the exact source
edit, selector, observed red result, restore operation, and restored hash.

Restoration protocol for every row: `git apply -R /tmp/wpwd-M<n>.patch`, then
`sha256sum <mutated-file>` and compare with the start hash shown in the row.
All rows restored cleanly; no production file is changed by the fixture round.

| Mutant | Exact patch | Collecting selector and observed red result | Start/restored hash |
|---|---|---|---|
| M1 | replace the complete live list comprehension in `_blockers_locked` with `return []` | `test_waiting_blocker_suppression_resolution_preserves_original_clock`; 1 failed (W emitted a stall while T remained outstanding) | `stalled_callback_watchdog.py` `3f8c0ad265f8a0711d484b2aab851844d99f9169c10664bda51868b640da7c08` |
| M2 | `oldest_inbound_at = current_episode.idle_since` replacing `min(episode.inbound_at ...)` | `test_waiting_safety_net_repeats_on_oldest_inbound_clock`; 1 failed (`IndexError`, no notice at 1000s) | same hash as M1 |
| M3 | add `current_episode.idle_since = now` in the blocker suppression branch | `test_waiting_blocker_suppression_resolution_preserves_original_clock`; 1 failed (idle clock changed from 0) | same hash as M1 |
| M4 | move the `if candidate.phase_p_waiting` gate below `_fresh_frame_decides_running` | `test_phase_p_waiting_skips_frame_and_defers_when_phase_a_clears`; 1 failed (`fresh.assert_not_called`) | same hash as M1 |
| M5 | remove `and not park_warm` from `_commit_watchdog_ops` | `test_parked_commit_still_settles_sender_and_never_clears_existing_episode`; 1 failed (parked receiver episode armed) | `inbox_service.py` `f8be540c14b393441e18876c19ed40db471ec90d2396838360928725fbb3b1c4` |
| M6 | add `and not episode.fired` to `_blockers_locked` | `test_fired_and_order_independent_blockers_and_membership_exits`; 1 failed (fired blocker no longer suppressed waiter) | `stalled_callback_watchdog.py` `3f8c0ad265f8a0711d484b2aab851844d99f9169c10664bda51868b640da7c08` |
| M7 | delete `if key in self._chain_notified: return None` | `test_notify_replaying_current_stall_persists_one_durable_chain_row`; 1 failed (`2 == 1` chain rows) | same hash as M1 |
| M8 | change chain notice `caller_id=worker_episode.caller_id` to `caller_id=notice.caller_id` | `test_notify_due_trigger_a_is_deduped_and_coalesces_trigger_b`; 1 failed (chain addressed to T, not C) | same hash as M1 |
| M9 | remove `bool(getattr(item, "park_warm", False))` from both `groupby` keys | `test_mixed_park_warm_batches_are_homogeneous_and_only_normal_arms`; 1 failed (true/false rows shared one delivery batch) | `inbox_service.py` `f8be540c14b393441e18876c19ed40db471ec90d2396838360928725fbb3b1c4` |
| M10 | same separated-clock patch as M2 | `test_waiting_safety_net_repeats_on_oldest_inbound_clock`; 1 failed (no 1000s notice and no 1600s repeat) | `stalled_callback_watchdog.py` `3f8c0ad265f8a0711d484b2aab851844d99f9169c10664bda51868b640da7c08` |
| M11 | delete Phase-A `if candidate.phase_p_waiting and not blockers: continue` recheck | `test_phase_p_empty_phase_a_waiting_suppresses_after_probes`; 1 failed (torn P/A read emitted W stall) | same hash as M1 |
| M12 | move `_reserve_chain_notice` after `_persist_notice` | `test_trigger_a_rollover_is_stale_and_insert_failure_rolls_back_reservation`; 1 failed (reservation survived insert failure) | same hash as M1 |
| M13 | omit logical `park_warm` when calling `_insert_routed_inbox_row` | `test_http_send_persists_park_warm_through_raw_and_logical_entry[True-True]`; 1 failed (logical row persisted false) | `mailbox_service.py` `cd49bec816f1fc8c8ac62b7ebc9f76b2fdbb537538692caaa97eac8f60287687` |
| M14 | replace `get_park_warm_for_message_ids(message_ids)` with `False` | `test_recovery_reads_persisted_member_park_warm[True]`; 1 failed (`commit ... False is True`) | `database.py` start/restored hash `fa604783f76b45bd054200f1810cec5d358e9c5263549fc3baa9cc4ac470bd99` |
| M15 | remove `or target_episode.generation != notice.source_generation` from `_reserve_chain_notice` | `test_trigger_a_rollover_is_stale_and_insert_failure_rolls_back_reservation`; 1 failed (stale generation emitted chain row) | same hash as M1 |
| M16 | delete the `jobs = [...]` A-over-B hold-and-filter comprehension in `notify_due` | `test_notify_due_trigger_a_is_deduped_and_coalesces_trigger_b`; 1 failed (waiting notice persisted alongside chain notice) | same hash as M1 |

Direct survivor probes collected at this build:

- Deferred `expect_callback=False` removal: 1 failed, watchdog episode present.
- Persisted `park_warm=False` replacement: 2 failed, true raw and logical rows read false.
- Recovery helper replacement: 1 failed, true member commit read false.
- Chain membership removal: 1 failed, two durable chain rows.
- Oldest-clock substitution: 1 failed, no separated-clock notice.

The fixture build's full suite is recorded in `tmp/orch/suite-wpwd-r2.log`:
`6850 passed, 25 skipped, 122 deselected, 9 warnings in 432.85s`.

Per-row restore commands (executed immediately after each red run):

```text
M1  git apply -R /tmp/wpwd-M1.patch
M2  git apply -R /tmp/wpwd-M2.patch
M3  git apply -R /tmp/wpwd-M3.patch
M4  git apply -R /tmp/wpwd-M4.patch
M5  git apply -R /tmp/wpwd-M5.patch
M6  git apply -R /tmp/wpwd-M6.patch
M7  git apply -R /tmp/wpwd-M7.patch
M8  git apply -R /tmp/wpwd-M8.patch
M9  git apply -R /tmp/wpwd-M9.patch
M10 git apply -R /tmp/wpwd-M10.patch
M11 git apply -R /tmp/wpwd-M11.patch
M12 git apply -R /tmp/wpwd-M12.patch
M13 git apply -R /tmp/wpwd-M13.patch
M14 git apply -R /tmp/wpwd-M14.patch
M15 git apply -R /tmp/wpwd-M15.patch
M16 git apply -R /tmp/wpwd-M16.patch
```
