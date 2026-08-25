# F459 Build Report — Native Callback Bridge Messages

## Branch & Tip
- **Branch:** `cao/a54c7e3c`
- **Tip:** `bf2a105c`
- **Parent:** `50a64f74` (F457 unified wake gating)

## Files Changed
| File | Change |
|------|--------|
| `src/cli_agent_orchestrator/services/cc_session_registry.py` | `build_wake_payload()`: new `message_body` + `sender_display_name` params; 8KB truncation; `from-name` = worker display name; `summary` attribute |
| `src/cli_agent_orchestrator/services/doorbell_service.py` | `ring_supervisor_doorbell()` + `_attempt_native_ring()`: forward F459 params; `_mark_socket_delivered()` + `is_socket_delivered()` via trace table |
| `src/cli_agent_orchestrator/services/inbox_service.py` | `CallbackRunOutcome` extended; both doorbell call sites wire content + display name |
| `test/services/test_f459_native_callback.py` | 17 tests across 5 classes |

## Test Results
- **F459 tests:** 17 passed, 0 failed (box@cursor, `uv run pytest`)
- **Existing doorbell regression:** 112 passed, 0 failed (test_fx170, test_fx168, test_f186)

## RED Evidence
Against parent `50a64f74`:
- `cc_session_registry.py`: 0 occurrences of `message_body` → param tests RED
- `doorbell_service.py`: 0 occurrences of `_mark_socket_delivered` → marker tests RED
- `from-name` in parent: `"cao-{sender_name}"` (prefixed) → from-name tests RED

## Design Decisions
1. **Marker via trace table** — no schema migration needed; uses existing `InboxMessageTraceEventModel` with `kind="f459.socket_delivered"`. Drain hook queries `is_socket_delivered(row_id)`.
2. **Display name resolution** — at doorbell call time, `utils.terminal.display_name(sender_id)` resolves `<profile>-<terminal_id>` from DB. Falls back to raw terminal ID on failure.
3. **Multi-row** — only the LAST written row's body is sent via native bridge (highest row_id); earlier rows surface via drain hook as before. Design choice: the native render shows the latest callback; the drain provides completeness.

## Root-Repo Hook Changes Needed
**`supervisor-inbox-drain.sh`** — needs one change: for each message being digested, check `is_socket_delivered(row_id)` and SKIP injecting that row's text into the `additionalContext` digest. The row is still ACKed (exactly-once stays hook-owned). Proposed approach: add a `cao messages is-socket-delivered <id>` CLI subcommand or HTTP endpoint that the hook calls, OR the hook queries the trace table directly via `python3 -c "..."`. The hook file itself is READ-ONLY for this worker — supervisor to decide implementation.

**`f213-callback-rewake.sh`** — no changes needed; the watcher polls for pending rows regardless of delivery channel.
