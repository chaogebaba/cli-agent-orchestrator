# FX191-S0 post-redeploy STRICT idle-wake probe

Probe terminal: `902a4893`
Supervisor: `7e6644ab`
Build: fork HEAD 5f131556 (F187+F188+F189)
Ring inbox id: `5446`
Pre-sleep: `2026-08-14T08:40:20.098Z`
Sleep start/end: `2026-08-14T08:40:24.713Z` → `2026-08-14T08:42:24.716Z` (120.24s real)
Ring send: `2026-08-14T08:42:31.132238` (MCP message_id 5446)

=== FINDINGS ===

verdict: PASS

Exactly one `delivery_obligation` row for the ring (inbox_row_id=5446).
Observed transition: created OPEN, later ACKED (terminal_reason=consumed).
No second obligation row, no leftover OPEN, no ESCALATED, no post-ACK re-OPEN.

Row:
- inbox_row_id: 5446
- mailbox_id: mb_1aa485bf
- state: ACKED
- accepted_at: 2026-08-14 08:42:31.134143
- first_attempt_at: 2026-08-14 08:42:33.851676
- terminal_at: 2026-08-14 08:42:54.894633
- attempts: 4
- next_attempt_at: 2026-08-14 08:42:55.427027
- terminal_reason: consumed
- inbox: sender=902a4893 receiver=7e6644ab status=delivered created_at=2026-08-14 08:42:31.132238

Anomalies (did not fail AC):
- attempts=4 while still OPEN; 4 fx191.resolve + 4 fx191.surface cycles ~5s apart before consume
- one fx191.transport_attempt deferred reason=no_registry_records count=4
- next_attempt_at remains populated after ACKED
- no fx191.ack/acked trace event; ACKED visible only on the obligation row
- ~23.76s accept→terminal (supervisor consume after idle-wake)

Repro query:

```sql
-- db: ~/.aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db
SELECT d.inbox_row_id, d.mailbox_id, d.state, d.accepted_at, d.first_attempt_at,
       d.terminal_at, d.attempts, d.next_attempt_at, d.terminal_reason,
       i.sender_id, i.receiver_id, i.status, i.created_at
FROM delivery_obligation d
JOIN inbox i ON i.id = d.inbox_row_id
WHERE i.message LIKE '%RING-FX191-S0%'
  AND i.sender_id = '902a4893'
  AND i.receiver_id = '7e6644ab';
```
