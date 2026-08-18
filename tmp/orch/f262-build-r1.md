# F262 — Quarantine Burn-Down Build Report R1

Artifact-Path: /home/chao/VScode_projects/cli-subagents/orchestrator/tmp/orch/f262-build-r1.md
Artifact-SHA256: (final)
Artifact-Repo-Path: tmp/orch/f262-build-r1.md
Git-SHA: 7724ff47910027cf8a70061935a5b3227ba2696c
Blueprint-SHA256: 4abba72fa2ff67c9bee784603da0764c9f8fb2cecc22b70ec152ed218296c5b2
Git-Branch: cao/f262
Base-Commit: b11c8acd

---

## Status: INFRASTRUCTURE COMPLETE + 6/22 ENTRIES RESOLVED

Remaining 14 `worker_crash` entries require suite-slot reproduction arms (D1 protocol: 20× `-n 2` per file). Suite slot requested from supervisor.

---

## AC Evidence

### AC1 — CAO_TEST_QUARANTINE=off makes plugin inert — GREEN

```
$ uv run pytest test/services/test_fifo_reader.py --collect-only -q -n 2 --dist loadgroup
29/31 tests collected (2 deselected)

$ CAO_TEST_QUARANTINE=off uv run pytest test/services/test_fifo_reader.py --collect-only -q -n 2 --dist loadgroup
31 tests collected
```

The 2 `worker_crash` entries (`test_data_received_across_writer_reconnects`, `test_stop_right_after_writer_eof_does_not_leak`) are deselected by default and present under `off`. No `xdist_group` marker, no `xfail` under `off`.

### AC2 — Default collection byte-identical — GREEN

```
Before F262 changes: 11707 tests collected
After F262 changes:  11707 tests collected
```

Zero difference in default collection.

### AC3 — serial_only + expires → FAIL — GREEN

Guard test `test_serial_only_schema` enforces: serial_only entries must NOT have `expires` key. A fixture-local entry with `expires` would fail the guard.

### AC4 — Unresolvable verdict → FAIL — GREEN

Guard test `test_serial_only_schema` parses `## ` headings from `quarantine-verdicts.md` and asserts every `verdict` value resolves.

### AC5 — serial_only is serialized, never deselected — GREEN

Under `-n 2 --dist loadgroup`, `serial_only` entries get `xdist_group("quarantine-serial")` marker. Under `-n 0` they run normally. Never deselected (verified: 5/5 telemetry tests collected regardless of mode).

### AC6 — Expiry guard fires for non-serial_only — GREEN

`test_expiry_guard_fires_for_non_serial_only` verifies all non-serial_only entries have valid, parseable expires. `test_no_expired_quarantine_entries` skips serial_only and checks the rest.

### AC7 — Departed entries have ledger sections — GREEN (mechanism in place)

`test_departed_entries_have_verdicts` cross-checks P4 nodeids against current registry. Currently SKIPPED (no entries have departed — converted entries are still in registry as `serial_only`). Will fire when entries are deleted.

---

## Entries Resolved (6/22)

| # | Entry | Bucket | Resolution |
|---|-------|--------|------------|
| 1 | `test_spans.py::test_emits_invoke_agent_with_required_attributes` | (b) | → `serial_only` (C1 OTel) |
| 2 | `test_spans.py::test_emits_execute_tool` | (b) | → `serial_only` (C1 OTel) |
| 3 | `test_spans.py::test_emits_chat_with_request_model` | (b) | → `serial_only` (C1 OTel) |
| 4 | `test_spans.py::test_chat_span_sets_conversation_id` | (b) | → `serial_only` (C1 OTel) |
| 5 | `test_auth.py::test_expected_audience_defaults_to_api_base_url_when_enabled` | (b) | → `serial_only` (C6 auth) |
| 6 | `test_auth.py::test_audience_fallback_enforced_in_validation` | (b) | → `serial_only` (C6 auth) |

---

## Remaining (14 worker_crash + 2 known_red)

Need D1 reproduction arms (suite slot). Grouped by file:
- `test_fifo_reader.py` (2 entries) — timing/threading
- `test_wpm4a_deferred_init_hardening.py` (2 entries) — timing
- `test_f72_fleet_lifecycle.py` (3 entries) — shared state / lease
- `test_claude_transcript_hook.py` (3 entries) — fixed filename collision
- `test_fold.py` (1 entry) — subprocess timeout
- `test_stage0_flip_machinery.py` (1 entry) — module-level state
- `test_ready_deadline_edge_probe.py` (1 entry) — timing
- `test_fx191_convergent_delivery.py` (1 entry) — timing
- 2 `known_red` (NG-1, out of scope)

---

## Files Modified

- `test/plugins/quarantine.py` — D1 `off` early return + D4 `serial_only` branch
- `test/test_f254_quarantine.py` — AC3/AC4/AC6/AC7 guard tests
- `test/quarantine.toml` — 6 entries converted to `serial_only`
- `test/quarantine-verdicts.md` — new, 6 verdict sections

---

## Mutation Kill (AC1 discriminator — M1)

If `off` were implemented as a synonym for `run` (M1), the telemetry tests under `off` would still carry `xdist_group("quarantine-serial")`. Verified:
- Under `off`: NO xdist_group marker (plugin returns early, never adds markers)
- Under `run`: xdist_group marker IS present (plugin processes entries)

The AC1 collection test discriminates between these: `off` collects all 31 tests flat (no group suffix in nodeids), while `run` shows `@quarantine-serial` suffixes.
