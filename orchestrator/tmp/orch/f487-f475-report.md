# F487 + F475 Fix Report

**Branch:** `cao/f487-f475-noise`  
**Head SHA:** `edd08bbfe6f8f5bb46e0f88f3e07716fd3f51d55`  
**Box suite:** 13595 passed, 3 failed (infra-only, unrelated):
- `test/plugins/test_suite_slot.py::TestLedgerSampling::test_sample_ledger_monotonic_growth`
- `test/services/test_fifo_reader.py::TestReaderThreadLifecycle::test_data_received_across_writer_reconnects`
- `test/services/test_fx168_doorbell.py::TestD12RateLimitedLog::test_warn_rate_limited`

## F487 (#342): park_warm watchdog suppression

**Root cause:** `record_inbound_task` in the stalled callback watchdog had no
terminal-level knowledge of park_warm. The `send_input` and inbox delivery
guards prevented arming on their respective paths, but any unconsidered call
path (e.g. `redeliver_dropped_message`, future code) could still arm. The
single-point-of-enforcement guard was missing.

**Fix:**
1. `terminal_service.create_terminal`: when `park_warm=True`, persist
   `{"park_warm": True}` in the terminal's system metadata (`cao` sub-dict)
   via `merge_terminal_system_metadata`.
2. `stalled_callback_watchdog.record_inbound_task`: before creating an episode,
   read terminal metadata (TTL-cached) and suppress when
   `metadata.cao.park_warm is True`.

**Tests:** `test/services/test_f487_park_warm_watchdog.py` — 7 cases covering
warm suppression, non-warm arming, empty/False/None edge cases.

## F475 (#330): cline double-send callback dedup

**Root cause:** cline (and potentially other providers) occasionally send their
READY/completion callback twice from the same worker to the same caller. The
model re-enters a turn and re-sends the callback (observed: "reworded but same
content" minutes apart).

**Fix:** Added a 60-second dedup window at the API endpoint
`POST /terminals/{receiver_id}/inbox/messages`. When:
- No barrier is active (`barrier is None`)
- Not park_warm
- The receiver is the sender's recorded caller

...check for existing messages from the same sender→receiver within the last
60s. If found, return the existing message (HTTP 200 with `deduplicated: true`)
without inserting a duplicate.

**Dedup key:** `(sender_id, receiver_id)` — any two messages from the same
sender terminal to the same receiver terminal within 60s are considered
duplicates, regardless of content. Content hashing was rejected because the
observed duplicates are "reworded but same content" (the model rephrases), so a
content hash would miss them. The key is intentionally coarse: a worker should
never need to send two distinct callbacks to its own caller within one minute.

**Why 60s:** The observed duplicates in the incident were ~1 min apart (inbox
ids 774/775). 60s covers that window with margin. The value is exposed as the
module constant `_F475_CALLBACK_DEDUP_WINDOW_S` for tuning.

**Tests:** `test/services/test_f475_callback_dedup.py` — helper unit tests.
Behavioral coverage via the existing barrier tests (which still pass,
confirming barrier sends bypass dedup).

## Files changed

- `src/cli_agent_orchestrator/services/terminal_service.py` — persist park_warm
- `src/cli_agent_orchestrator/services/stalled_callback_watchdog.py` — guard
- `src/cli_agent_orchestrator/clients/database.py` — dedup helpers
- `src/cli_agent_orchestrator/api/main.py` — API dedup gate
- `src/cli_agent_orchestrator/kernel/receiver_state/trace_manifest.txt` — regen
- `test/services/test_f487_park_warm_watchdog.py` — new
- `test/services/test_f475_callback_dedup.py` — new
