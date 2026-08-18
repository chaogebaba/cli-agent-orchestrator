# Quarantine Verdicts Ledger (F262)

Append-only. One `##` section per entry that leaves the registry.
Format: nodeid heading, bucket tag, root cause, arm counts, fix commit or retirement date.

---

## test_emits_invoke_agent_with_required_attributes

- **Bucket:** (b) test-fixture bug
- **Root cause:** Process-global OTel `TracerProvider` + shared `InMemorySpanExporter` at `test/telemetry/conftest.py:31-39`. Under `-n 2`, non-quarantined OTel tests on the same worker add spans to the exporter, breaking `len(finished) == 1` assertions.
- **Arm counts:** A 3/3 green (serial passes), B 20/20 green (file-only), C identified co-schedulers in `test/telemetry/`
- **Fix:** Serialize via `serial_only` (process-global OTel provider is un-isolatable per `conftest.py:3-5`)
- **Date:** 2026-08-18

## test_emits_execute_tool

- **Bucket:** (b) test-fixture bug
- **Root cause:** Same as `test_emits_invoke_agent_with_required_attributes` — shared `InMemorySpanExporter`.
- **Fix:** Serialize via `serial_only`
- **Date:** 2026-08-18

## test_emits_full_exec_span_on_successful_result

- **Bucket:** (b) test-fixture bug
- **Root cause:** Same OTel cluster (C1).
- **Fix:** Serialize via `serial_only`
- **Date:** 2026-08-18

## test_real_span_export_to_memory_exporter

- **Bucket:** (b) test-fixture bug
- **Root cause:** Same OTel cluster (C1).
- **Fix:** Serialize via `serial_only`
- **Date:** 2026-08-18

## test_emits_chat_with_request_model

- **Bucket:** (b) test-fixture bug
- **Root cause:** Same OTel cluster (C1) — process-global TracerProvider.
- **Fix:** Serialize via `serial_only`
- **Date:** 2026-08-18

## test_chat_span_sets_conversation_id

- **Bucket:** (b) test-fixture bug
- **Root cause:** Same OTel cluster (C1) — process-global TracerProvider.
- **Fix:** Serialize via `serial_only`
- **Date:** 2026-08-18

## test_expected_audience_defaults_to_api_base_url_when_enabled

- **Bucket:** (b) test-fixture bug
- **Root cause:** Cross-worker `monkeypatch` of module-level auth constants (`test/security/test_auth.py`). Under xdist, two workers mutate `auth._audience` concurrently.
- **Arm counts:** A 3/3 green, B 20/20 green (file-only serialization resolves)
- **Fix:** Serialize via `serial_only`
- **Date:** 2026-08-18

## test_audience_fallback_enforced_in_validation

- **Bucket:** (b) test-fixture bug
- **Root cause:** Same as `test_expected_audience_defaults_to_api_base_url_when_enabled` — cross-worker auth constant mutation.
- **Fix:** Serialize via `serial_only`
- **Date:** 2026-08-18
