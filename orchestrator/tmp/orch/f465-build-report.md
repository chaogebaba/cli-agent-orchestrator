# F465 Build Report — Orphan Scanner False-Alarm Filter

## Summary

Added `_is_descendant_of_server(pid)` to the F138 orphan scanner's
PermissionError decision tree. Same-UID processes whose parent chain reaches
PID 1 (init) without passing through the CAO server PID cannot carry an
incarnation token — they are annotated `not_server_descendant` and no longer
mark the scan incomplete. Eliminates the standing false-alarm noise from
user-session processes (systemd --user children like sd-pam) on every terminal
reap.

## Changes (3 files)

| File | Anchor | Change |
|------|--------|--------|
| `src/cli_agent_orchestrator/services/orphan_reconcile_service.py:338` | `_is_descendant_of_server()` | New helper: walks pid's parent chain upward; returns True if server PID found before init |
| `src/cli_agent_orchestrator/services/orphan_reconcile_service.py:372` | Fail-closed broken-chain path | `return True` on OSError/cycle/unparseable — assumes potentially dangerous when chain unresolvable |
| `src/cli_agent_orchestrator/services/orphan_reconcile_service.py:266-278` | `not_server_descendant` annotation | In scan_incarnation_processes PermissionError branch: after predate check fails, calls `_is_descendant_of_server`; if False → annotate + continue (preserves completeness) |
| `test/services/test_f465_orphan_scanner_false_alarm.py` | New file | 11 tests covering the false-alarm fix and real-orphan detection |
| `test/services/test_f138_orphan_reconciliation.py` | D17 mock addition | 3 existing tests updated with `_is_descendant_of_server=True` mock |

## New Test Roster (test_f465_orphan_scanner_false_alarm.py — 11 tests)

### TestF465FalseAlarmGone (false-alarm class eliminated)
1. `test_sd_pam_sibling_preserves_completeness` — sd-pam (child of systemd --user, sibling of server, postdates issuance) → benign via not_server_descendant, scan complete
2. `test_multiple_user_session_procs_all_benign` — multiple user-session processes (sd-pam, dbus-daemon, pipewire) all classified benign
3. `test_non_descendant_without_issuance_ticks` — non-descendant with issuance_ticks=None (core reproduction path from #320) → still benign

### TestF465RealOrphanStillFlagged (safety preserved)
4. `test_server_descendant_unreadable_environ_incomplete` — process IS a child of server, postdates issuance, unreadable environ → scan_incomplete (correctly flagged)
5. `test_server_descendant_predates_issuance_is_clean` — server descendant that predates issuance → still clean via predates_issuance path

### TestIsDescendantOfServer (helper unit tests)
6. `test_direct_child_is_descendant` — direct child of server → True
7. `test_grandchild_is_descendant` — grandchild of server → True
8. `test_sibling_not_descendant` — sibling of server (child of same parent) → False
9. `test_server_itself_not_descendant` — server PID → False
10. `test_ancestor_not_descendant` — server ancestor (systemd --user) → False
11. `test_unrelated_process_not_descendant` — broken parent chain → True (fail closed)

## Updated D17 Tests (3 tests)

These tests simulate the "same-UID, not-ancestor, postdate/unreadable/boot-mismatch"
path and assert `complete=False`. Post-F465, the new descendant check would classify
them as benign (their mock proc trees lead to init without the server PID). Adding
`_is_descendant_of_server=True` is correct because the scenario they test IS a real
orphan — a process that IS a server descendant with unreadable environ that cannot be
proven safe by other means.

- `TestD17PreIssuanceFence::test_postdate_remains_incomplete`
- `TestD17PreIssuanceFence::test_stat_unreadable_remains_incomplete`
- `TestD17PreIssuanceFence::test_boot_unavailable_remains_incomplete`

## Verification (box@cursor-3)

```
test_f465_orphan_scanner_false_alarm.py  11 passed in 0.54s
test_f138_orphan_reconciliation.py       97 passed in 5.99s
test_f138_r6_issuance_scan.py            11 passed in 0.34s
```
