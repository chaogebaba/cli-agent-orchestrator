# F476 Build Report — Single Wake Cursor (post-gate-r2-fix)

Branch: `cao/f476-build` (head: 5d4355aa)
Base: `main` (a70bd41a)

## Gate-r2 fix tally

| Blocker | Fix | Detail |
|---------|-----|--------|
| B1-r2 | F351 fast path + fx168 stale-path | F351: read-only pre-check exits without BEGIN IMMEDIATE on empty mailbox. fx168: push runner compares mailbox path vs terminal metadata and returns stale_path_detected + needs_immediate_wake without emitting. |
| B2-r2 | Lease epoch identity | New `wake_lease_epoch` column increments on each claim. commit_wake verifies `current_epoch == lease_epoch`; stale epoch → lease_lost. |
| B3-r2 | 300s visibility interval | commit_wake re-stamps `wake_notified_at` (not NULL) and sets `wake_notified_id = through_id`. Subsequent claims within 300s get lease_held for committed-pending rows. |
| B5-r2 | Driven exhaustion + once-only WARNING + wired alarm | Recovery commits (through_id == cursor, no replay) increment wake_streak. WARNING deduped via `_wake_exhaustion_warned` set. `get_wake_exhaustion_alarms()` called in `build_fleet()`. |

## Box suite verdict

```
310 passed, 4 xfailed in 17.21s
```

(Same 17-file affected suite as prior runs. xfails are pre-existing on main.)

## Per-blocker regression tests

- B1-r2 F351: AC3 test now verifies lease_held immediately after commit (no BEGIN IMMEDIATE for empty claim)
- B1-r2 fx168: Push runner returns stale_path_detected when metadata path differs
- B2-r2/AC14: TestAC14 stale commit after successor claim+commit returns lease_lost (epoch mismatch)
- B3-r2: TestAC3 verifies immediate re-claim after commit → lease_held; recovery only after 310s clock advance
- B5-r2: TestAC12 streak_increments uses clock + replay to drive streak; exhaustion WARNING deduped

## Commits on lane

```
5d4355aa F476 gate-r2 fixes: B1-r2/B2-r2/B3-r2/B5-r2
859b2c7b F476 B6 followup: fix remaining test breakage from stub removal
cb0712ee F476 B6: AC6 exact legacy cursor retirement — 3/3 grep counts
77f576fd F476 B1: route F136 push runner through claim_unnotified_wake/commit_wake
6795e1ae F476 gate fixes B2-B5/B7: bound commit, recovery, lease, exhaustion, drain auth
```
(plus original 5 pre-gate commits)
