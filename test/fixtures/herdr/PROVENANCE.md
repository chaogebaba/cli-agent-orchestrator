# herdr H1 test fixtures — provenance

All fixtures here are copied or distilled from the H0 round-2 probe evidence,
which ran against a real herdr **0.9.0** release binary
(sha256 `4fa1a01158dd8043da92d31b270780b0dcc10603038d9b61cac4d81ab63fb71f`,
**protocol 22 / schema_version 1**) on grok-box-007.

- Evidence root (laptop copy): `/data/cao-scratch/herdr-p0-r2-evidence/`
- Report: `/data/cao-scratch/briefs/herdr-phase0-report-r2.md`
  (sha256 `48493caf…fbf0a`)
- Probe scripts: `probes/herdr-p0-r2/`

| Fixture | Source in evidence | What it is |
| --- | --- | --- |
| `pi-events.jsonl` | `p2/pi/events.jsonl` (verbatim) | The real subscribed socket event stream for the pi lane: line 1 is the `events.subscribe` ack (`{"result":{"type":"subscription_started"}}`), then `pane_agent_detected`, then broadcast `pane_updated` events nesting the pane under `data.pane` with `agent_status`. Includes `_recv_ms`/`_recv_iso` receive stamps the probe added. |
| `subscribe-ack.json` | `p2/pi/subscribe-ack.json` (verbatim) | The `events.subscribe` acknowledgement in isolation. |
| `pane-records.json` | distilled | Real herdr pane/agent records at each status, keyed by status: `working`/`done` are `agent` records from `p2/pi/turn-05/{mid-working,wait-idle}.json` (they carry `screen_detection_skipped=true` — hook-backed pi); `idle`/`unknown` are `data.pane` records lifted from `p2/pi/events.jsonl`. All share `terminal_id=term_65b015bb41ad32`, `pane_id=w2:p1`, and an `agent_session` identity handle. |
| `api-schema-head.json` | distilled from `p1/api-schema.json` | The `$schema`/`protocol`/`schema_version` header of `herdr api schema --json` (protocol 22, schema_version 1) — the pin `HerdrClient.check_protocol` enforces. Full schema is large; only the pinned header is kept. |
| `api-schema-summary.txt` | `p1/api-schema-summary.txt` (verbatim) | Human summary confirming protocol 22 / schema_version 1. |
| `status.txt` | `p1/status.txt` (verbatim) | `herdr status`: client+server 0.9.0, protocol 22, endpoint compatible. |

These fixtures replay herdr's real 0.9.0 wire shapes so the client and adapter
unit tests exercise the actual JSON the server emits, not invented shapes.
