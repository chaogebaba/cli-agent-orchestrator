# F702 (#557) fleet TUI fixtures

Seven recorded payloads for the `cao-fleet` renderer tests (blueprint D5, AC2).
Five are `build_fleet()`-shaped snapshots; two are **failure markers** that carry
no payload, because the case they describe is "the fetch produced nothing".

| file | case |
|---|---|
| `healthy.json` | supervisor plus a working and a completed worker |
| `error_latched.json` | #439 shape: `status="error"` while `init_state`/`init_health` stayed `ready`; one of the two also carries `condition="CAPPED"` |
| `delegating.json` | F568 D12c: an idle seat with `delegating=true`, `children_count=2`, and its two working children |
| `wake_alarm.json` | non-empty `wake_exhaustion_alarms`, plus a `wedge_suspect` + `config_stale` worker |
| `empty_session.json` | a session with no terminals |
| `fetch_timeout.json` | marker: the fetch timed out after two consecutive failures; no payload |
| `never_fetched.json` | marker: app start, nothing has arrived yet; no payload |

## Shape

A payload fixture is exactly what `GET /sessions/<name>/fleet` returns
(`services/fleet_service.py`, `build_fleet()`): `session_name`, `terminals`, and
`wake_exhaustion_alarms`. Every terminal carries the full projected key set —
`id`, `profile`, `provider`, `window_index`, `window_name`, `parent_id`, `depth`,
`orphan`, `status`, `condition`, `fusion_changed`, `fusion_reason`, `delegating`,
`children_count`, `init_state`, `init_health`, `since_last_input`, `lifecycle`,
`resolved_model`, `reparented_from`, `config_stale`, `wedge_suspect`.
`test/tui/test_status_cell.py` asserts that key set, so a server-side projection
change that adds or drops a key fails here first.

A marker fixture has `"__fixture__"` set (`fetch_failure` or `never_fetched`),
`"payload": null`, and the fields the fetcher records about the failure:
`error_class`, `error`, `consecutive_failures`, plus a `note` describing what the
renderer must show. Markers are not fleet payloads; never feed one to a renderer
as if it were.

## Re-recording from a live session

```bash
curl -s "$CAO_ENDPOINT/sessions/<session-name>/fleet" | python3 -m json.tool \
  > test/tui/fixtures/<case>.json
```

`CAO_ENDPOINT` is the running `cao-server` (`http://127.0.0.1:9889` by default);
`<session-name>` is the tmux/CAO session, the same name `cao-fleet --session`
takes. Scrub anything session-specific you do not want committed: terminal ids,
`resolved_model`, and `session_name` are all safe to rewrite by hand, and the
tests only depend on the key set and on the values each case is named for.

The two markers cannot be recorded from the endpoint — write them by hand, or
copy an existing one and edit the failure fields.
