import hashlib
import json
import os
import shutil
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.services import wpd1_decontam as service


FIXTURES = Path(__file__).parents[1] / "providers" / "fixtures"
POSITIVE = FIXTURES / "wpd1_artifact_refusal_echo_positive_2026_07_17.jsonl"
CONTROL = FIXTURES / "wpd1_artifact_conversation_zero_2026_07_17.jsonl"
POSITIVE_UUID = "019f71ad-d32e-7fc2-9697-cf294e365424"
CONTROL_UUID = "019f71bd-d2dc-7610-9fe9-c3e3fccc0202"


def _value(allowed):
    for kind in ("string", "integer", "bool", "object", "array", "number", "null"):
        if kind in allowed:
            return {
                "string": "value",
                "integer": 1,
                "bool": True,
                "object": {},
                "array": [],
                "number": 1.5,
                "null": None,
            }[kind]
    raise AssertionError(allowed)


def _record(family, variant=None, *, include_optional=False):
    shape = service.SCHEMA_0144[family][variant]
    payload = {name: _value(types) for name, types in shape["required"].items()}
    if include_optional:
        payload.update({name: _value(types) for name, types in shape["optional"].items()})
    if variant is not None:
        payload["type"] = variant
    if family == "session_meta":
        payload["id"] = "session-uuid"
        payload["cli_version"] = "0.144.1"
    return {"timestamp": "2026-01-01T00:00:00Z", "type": family, "payload": payload}


ALL_SHAPES = [
    (family, variant)
    for family, variants in service.SCHEMA_0144.items()
    for variant in variants
]
OPTIONAL_SHAPES = [
    (family, variant)
    for family, variant in ALL_SHAPES
    if service.SCHEMA_0144[family][variant]["optional"]
]


def test_frozen_schema_table_cardinality():
    assert len(service.SCHEMA_0144) == 7
    assert sum(len(v) for family, v in service.SCHEMA_0144.items()
               if family in {"event_msg", "response_item"}) == 22
    assert len(OPTIONAL_SHAPES) == 8


@pytest.mark.parametrize(("family", "variant"), ALL_SHAPES)
def test_every_frozen_family_and_variant_validates(family, variant):
    service.validate_record(_record(family, variant))


@pytest.mark.parametrize(("family", "variant"), OPTIONAL_SHAPES)
def test_every_multi_shape_group_accepts_its_optional_fields(family, variant):
    service.validate_record(_record(family, variant, include_optional=True))


@pytest.mark.parametrize(("family", "variant"), ALL_SHAPES)
def test_each_shape_rejects_missing_required_unknown_extra_and_wrong_type(family, variant):
    record = _record(family, variant)
    required = next(iter(service.SCHEMA_0144[family][variant]["required"]))
    missing = json.loads(json.dumps(record))
    del missing["payload"][required]
    missing_error = (
        "nested_discriminator_missing" if required == "type" and variant is not None
        else "required_payload_field_missing"
    )
    with pytest.raises(service.DecontaminationError, match=missing_error):
        service.validate_record(missing)

    extra = json.loads(json.dumps(record))
    extra["payload"]["future_field"] = "value"
    with pytest.raises(service.DecontaminationError, match="unknown_payload_field"):
        service.validate_record(extra)

    wrong = json.loads(json.dumps(record))
    wrong["payload"][required] = {"wrong": True}
    if "object" in service.SCHEMA_0144[family][variant]["required"][required]:
        wrong["payload"][required] = "wrong"
    wrong_error = (
        "nested_discriminator_missing" if required == "type" and variant is not None
        else "payload_field_type_invalid"
    )
    with pytest.raises(service.DecontaminationError, match=wrong_error):
        service.validate_record(wrong)


@pytest.mark.parametrize(
    ("family", "variant"),
    [("response_item", "message"), ("event_msg", "agent_message")],
)
def test_unknown_family_and_each_nested_variant_family_fail_closed(family, variant):
    with pytest.raises(service.DecontaminationError, match="unknown_top_level_family"):
        service.validate_record({"timestamp": "t", "type": "future", "payload": {}})
    record = _record(family, variant)
    record["payload"]["type"] = "future_variant"
    with pytest.raises(service.DecontaminationError, match="unknown_nested_discriminator"):
        service.validate_record(record)


def test_version_mismatch_fails_closed():
    record = _record("session_meta")
    record["payload"]["id"] = "uuid"
    record["payload"]["cli_version"] = "0.145.0"
    content = (json.dumps(record) + "\n").encode()
    with pytest.raises(service.DecontaminationError, match="artifact_version_unsupported"):
        service.validate_artifact_bytes(content, "uuid", "rollout-uuid.jsonl")


def test_tracked_fixture_hashes_and_artifact_rule_controls():
    assert hashlib.sha256(POSITIVE.read_bytes()).hexdigest() == (
        "5bc0f81ae6770713565ed0b6ccd73b7f11140a7de4cf8e91e6298d819fdf6e07"
    )
    assert hashlib.sha256(CONTROL.read_bytes()).hexdigest() == (
        "b012a58b0a8c95cd230d54ba69be2aa32bbc21877e225c7d64934a37060154b4"
    )
    positive_records = service.validate_artifact_bytes(
        POSITIVE.read_bytes(), POSITIVE_UUID, f"rollout-{POSITIVE_UUID}.jsonl"
    )
    control_records = service.validate_artifact_bytes(
        CONTROL.read_bytes(), CONTROL_UUID, f"rollout-{CONTROL_UUID}.jsonl"
    )
    positive_spans = service.discover_artifact_spans(positive_records)
    assert len(positive_spans) >= 1
    assert {span.rule_id for span in positive_spans} == {
        service.CONTENT_POLICY_ARTIFACT_RULE_ID
    }
    assert service.discover_artifact_spans(control_records) == ()


def test_denied_sibling_content_is_not_an_artifact_candidate():
    records = service.validate_artifact_bytes(
        POSITIVE.read_bytes(), POSITIVE_UUID, f"rollout-{POSITIVE_UUID}.jsonl"
    )
    source = next(value for _, _, value in service._decoded_candidates(records)
                  if service._artifact_pattern().search(value))
    denied = _record("event_msg", "agent_message")
    denied["payload"]["message"] = source
    assert service.discover_artifact_spans([denied]) == ()


def test_artifact_match_overflow_rejects_the_whole_plan():
    records = service.validate_artifact_bytes(
        POSITIVE.read_bytes(), POSITIVE_UUID, f"rollout-{POSITIVE_UUID}.jsonl"
    )
    source = next(value for _, _, value in service._decoded_candidates(records)
                  if service._artifact_pattern().search(value))
    record = _record("response_item", "custom_tool_call")
    record["payload"]["input"] = " ".join([source] * 65)
    with pytest.raises(service.DecontaminationError, match="match_limit"):
        service.discover_artifact_spans([record])


def _incident(*, status="not_attempted", skip=None, message_id=None, stage="backup"):
    return {
        "record_version": 1,
        "terminal_id": "term",
        "lifecycle_generation": 1,
        "provider": "codex",
        "provider_session_uuid": "uuid",
        "rule_id": service.CONTENT_POLICY_SCREEN_RULE_ID,
        "classified_reason": service.CLASSIFIED_REASON,
        "invoker": "human-cli",
        "caller_snapshot": {"mailbox_id": None, "incarnation_terminal_id": None},
        "gating_basis": "test",
        "force": False,
        "prior_incident": None,
        "attempts": [{
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": None,
            "final_stage": stage,
            "result": "aborted",
            "scrub_summary": None,
        }],
        "nudge_status": status,
        "nudge_skip_reason": skip,
        "nudge_message_id": message_id,
    }


@pytest.mark.parametrize("stage", sorted(service.FINAL_STAGES))
def test_every_closed_stage_value_is_valid(stage):
    service.validate_incident_record(_incident(stage=stage))


def test_unknown_incident_stage_is_rejected():
    with pytest.raises(service.DecontaminationError, match="stage_invalid"):
        service.validate_incident_record(_incident(stage="future"))


def test_incident_rejects_unknown_top_level_field_and_unregistered_rule_identity():
    unknown = _incident()
    unknown["future_field"] = "value"
    wrong_rule = _incident()
    wrong_rule["rule_id"] = "codex.screen.grammar-valid-but-unregistered.v1"
    with pytest.raises(service.DecontaminationError, match="incident_fields_invalid"):
        service.validate_incident_record(unknown)
    with pytest.raises(service.DecontaminationError, match="incident_rule_invalid"):
        service.validate_incident_record(wrong_rule)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("started_at", "not-a-timestamp", "started_invalid"),
        ("finished_at", "2026-01-01T01:00:00+01:00", "finished_invalid"),
    ],
)
def test_incident_attempt_timestamps_must_parse_as_iso8601_utc(field, value, error):
    record = _incident()
    record["attempts"][0][field] = value
    with pytest.raises(service.DecontaminationError, match=error):
        service.validate_incident_record(record)


@pytest.mark.parametrize(
    "record",
    [
        _incident(status="sent", message_id=7),
        _incident(status="failed"),
        _incident(status="skipped", skip="no-nudge-flag"),
        _incident(status="skipped", skip="caller-unresolvable"),
        _incident(status="not_attempted"),
    ],
)
def test_incident_nudge_matrix_accepts_only_frozen_states(record):
    service.validate_incident_record(record)


def test_incident_nudge_field_presence_rejects_all_four_directions():
    cases = []
    present_non_sent = _incident(status="failed", message_id=3)
    cases.append(present_non_sent)
    missing_sent = _incident(status="sent", message_id=3)
    del missing_sent["nudge_message_id"]
    cases.append(missing_sent)
    missing_skipped = _incident(status="skipped", skip="no-nudge-flag")
    del missing_skipped["nudge_skip_reason"]
    cases.append(missing_skipped)
    present_non_skipped = _incident(status="failed", skip="no-nudge-flag")
    cases.append(present_non_skipped)
    for record in cases:
        with pytest.raises(service.DecontaminationError):
            service.validate_incident_record(record)


@pytest.mark.parametrize(
    "value",
    [
        {"status": "sent", "nudge_message_id": 1},
        {"status": "failed"},
        {"status": "skipped", "skip_reason": "no-nudge-flag"},
        {"status": "not_attempted"},
    ],
)
def test_response_nudge_objects_follow_conditional_presence(value):
    assert service.validate_nudge_object(value) == value


@pytest.mark.parametrize(
    "value",
    [
        {"status": "failed", "nudge_message_id": 1},
        {"status": "sent"},
        {"status": "skipped"},
        {"status": "failed", "skip_reason": "no-nudge-flag"},
    ],
)
def test_response_nudge_objects_reject_all_field_presence_violations(value):
    with pytest.raises(ValueError):
        service.validate_nudge_object(value)


def test_incident_mutations_compose_under_flock(tmp_path):
    incident_dir = service.incident_directory("term", 1, log_dir=tmp_path)
    service.mutate_incident(incident_dir, lambda _current: _incident())
    barrier = threading.Barrier(2)

    def update_field(field, value):
        barrier.wait()

        def update(current):
            record = dict(current)
            record[field] = value
            return record

        service.mutate_incident(incident_dir, update)

    first = threading.Thread(target=update_field, args=("invoker", "supervisor"))
    second = threading.Thread(target=update_field, args=("gating_basis", "basis"))
    first.start(); second.start(); first.join(); second.join()
    record = json.loads((incident_dir / "incident.json").read_text())
    assert record["invoker"] == "supervisor"
    assert record["gating_basis"] == "basis"


def _install_fixture_home(monkeypatch, tmp_path):
    sessions = tmp_path / "provider-home" / "sessions" / "2026" / "07" / "17"
    sessions.mkdir(parents=True)
    artifact = sessions / f"rollout-{POSITIVE_UUID}.jsonl"
    shutil.copyfile(POSITIVE, artifact)
    monkeypatch.setattr(service, "provider_home", lambda _provider: SimpleNamespace(sessions=sessions))
    return artifact


def _prepare(monkeypatch, tmp_path, **kwargs):
    artifact = _install_fixture_home(monkeypatch, tmp_path)
    prepared = service.prepare_content_recovery(
        terminal_id="term",
        lifecycle_generation=1,
        session_uuid=POSITIVE_UUID,
        invoker="supervisor",
        caller_mailbox_id="mb_1",
        caller_terminal_id="caller",
        gating_basis="test-basis",
        force=False,
        show=True,
        use_cpa=False,
        log_dir=tmp_path / "logs",
        **kwargs,
    )
    return artifact, prepared


def test_prepare_install_backup_audit_and_repeated_success_fence(monkeypatch, tmp_path):
    artifact, prepared = _prepare(monkeypatch, tmp_path)
    try:
        assert artifact.read_bytes() == prepared.scrub_result.content
        assert prepared.backup_path.read_bytes() == POSITIVE.read_bytes()
        assert stat.S_IMODE(prepared.incident_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(prepared.backup_path.stat().st_mode) == 0o600
        assert stat.S_IMODE((prepared.incident_path.parent / "incident.lock").stat().st_mode) == 0o600
        service.mark_recovery_complete(prepared)
    finally:
        service.release_prepared_recovery(prepared)
    with pytest.raises(service.RepeatedIncident):
        service.prepare_content_recovery(
            terminal_id="term", lifecycle_generation=1, session_uuid=POSITIVE_UUID,
            invoker="supervisor", caller_mailbox_id="mb_1",
            caller_terminal_id="caller", gating_basis="retry", force=False,
            show=False, use_cpa=False, log_dir=tmp_path / "logs",
        )


@pytest.mark.parametrize("prior_result", ["failed", "aborted"])
def test_failed_and_aborted_attempts_do_not_fence(monkeypatch, tmp_path, prior_result):
    _install_fixture_home(monkeypatch, tmp_path)
    log_dir = tmp_path / "logs"
    incident_dir = service.incident_directory("term", 1, log_dir=log_dir)
    prior = _incident()
    prior["attempts"][0]["result"] = prior_result
    prior["attempts"][0]["finished_at"] = "2026-01-01T00:00:01+00:00"
    service.mutate_incident(incident_dir, lambda _current: prior)

    prepared = service.prepare_content_recovery(
        terminal_id="term", lifecycle_generation=1, session_uuid=POSITIVE_UUID,
        invoker="supervisor", caller_mailbox_id="mb_1",
        caller_terminal_id="caller", gating_basis="retry", force=False,
        show=False, use_cpa=False, log_dir=log_dir,
    )
    try:
        record = json.loads(prepared.incident_path.read_text())
        assert len(record["attempts"]) == 2
    finally:
        service.restore_backup(prepared)
        service.release_prepared_recovery(prepared)


def _complete_then_restore(prepared):
    service.mark_recovery_complete(prepared)
    service.restore_backup(prepared)
    service.release_prepared_recovery(prepared)


def test_force_override_and_new_generation_bypass_only_the_exact_fence(monkeypatch, tmp_path):
    artifact, first = _prepare(monkeypatch, tmp_path)
    first_incident_dir = first.incident_path.parent
    _complete_then_restore(first)
    assert artifact.read_bytes() == POSITIVE.read_bytes()

    forced = service.prepare_content_recovery(
        terminal_id="term", lifecycle_generation=1, session_uuid=POSITIVE_UUID,
        invoker="human-cli", caller_mailbox_id="mb_1", caller_terminal_id="caller",
        gating_basis="human-force", force=True, show=False, use_cpa=False,
        log_dir=tmp_path / "logs",
    )
    try:
        forced_record = json.loads(forced.incident_path.read_text())
        assert forced_record["force"] is True
        assert forced_record["prior_incident"] == str(forced.incident_path)
    finally:
        service.restore_backup(forced)
        service.release_prepared_recovery(forced)

    next_generation = service.prepare_content_recovery(
        terminal_id="term", lifecycle_generation=2, session_uuid=POSITIVE_UUID,
        invoker="supervisor", caller_mailbox_id="mb_1", caller_terminal_id="caller",
        gating_basis="new-generation", force=False, show=False, use_cpa=False,
        log_dir=tmp_path / "logs",
    )
    try:
        assert next_generation.incident_path.parent != first_incident_dir
    finally:
        service.restore_backup(next_generation)
        service.release_prepared_recovery(next_generation)


def test_incident_identity_distinguishes_rule_id(tmp_path):
    first = service.incident_directory(
        "term", 1, service.CONTENT_POLICY_SCREEN_RULE_ID, log_dir=tmp_path
    )
    second = service.incident_directory(
        "term", 1, "codex.screen.future-amended-rule.v2", log_dir=tmp_path
    )
    assert first != second


def test_post_initialize_nonappend_restores_but_retains_backup(monkeypatch, tmp_path):
    artifact, prepared = _prepare(monkeypatch, tmp_path)
    backup = prepared.backup_path
    try:
        assert service.post_initialize_failure(prepared, "resume") is True
        assert artifact.read_bytes() == POSITIVE.read_bytes()
        assert backup.exists()
    finally:
        service.release_prepared_recovery(prepared)


def test_post_initialize_append_preserves_newer_artifact(monkeypatch, tmp_path):
    artifact, prepared = _prepare(monkeypatch, tmp_path)
    appended = (json.dumps(_record("world_state"), separators=(",", ":")) + "\n").encode()
    with artifact.open("ab") as stream:
        stream.write(appended)
    newest = artifact.read_bytes()
    try:
        assert service.post_initialize_failure(prepared, "resume") is False
        assert artifact.read_bytes() == newest
    finally:
        service.release_prepared_recovery(prepared)


def test_plan_replace_drift_two_actor_barrier_aborts_without_overwrite(monkeypatch, tmp_path):
    artifact = _install_fixture_home(monkeypatch, tmp_path)
    decision_boundary = threading.Barrier(2)
    writer_done = threading.Event()
    original_validate_lease = service._validate_artifact_lease
    errors = []

    def pause_at_decision(lease):
        decision_boundary.wait()
        assert writer_done.wait(timeout=2)
        original_validate_lease(lease)

    def scrubber():
        try:
            service.prepare_content_recovery(
                terminal_id="term", lifecycle_generation=1, session_uuid=POSITIVE_UUID,
                invoker="supervisor", caller_mailbox_id=None, caller_terminal_id=None,
                gating_basis="race", force=False, show=False, use_cpa=False,
                log_dir=tmp_path / "logs",
            )
        except Exception as exc:
            errors.append(exc)

    def competing_writer():
        decision_boundary.wait()
        artifact.write_bytes(
            POSITIVE.read_bytes() + (json.dumps(_record("world_state")) + "\n").encode()
        )
        writer_done.set()

    monkeypatch.setattr(service, "_validate_artifact_lease", pause_at_decision)
    actors = [threading.Thread(target=scrubber), threading.Thread(target=competing_writer)]
    for actor in actors:
        actor.start()
    for actor in actors:
        actor.join(timeout=3)
    assert all(not actor.is_alive() for actor in actors)
    assert len(errors) == 1
    assert isinstance(errors[0], service.DecontaminationError)
    assert "artifact_drift_before_install" in str(errors[0])
    assert len(artifact.read_bytes()) > len(POSITIVE.read_bytes())


def test_artifact_lease_two_actor_race_allows_exactly_one_scrub(monkeypatch, tmp_path):
    _install_fixture_home(monkeypatch, tmp_path)
    start = threading.Barrier(3)
    winner_at_plan = threading.Event()
    loser_done = threading.Event()
    original_find = service.find_artifact
    results = []
    errors = []

    def hold_winner(session_uuid):
        winner_at_plan.set()
        assert loser_done.wait(timeout=2)
        return original_find(session_uuid)

    def actor(name):
        start.wait()
        prepared = None
        try:
            prepared = service.prepare_content_recovery(
                terminal_id=name, lifecycle_generation=1, session_uuid=POSITIVE_UUID,
                invoker="supervisor", caller_mailbox_id=None, caller_terminal_id=None,
                gating_basis="race", force=False, show=False, use_cpa=False,
                log_dir=tmp_path / "logs",
            )
            results.append(name)
        except Exception as exc:
            errors.append(exc)
            loser_done.set()
        finally:
            service.release_prepared_recovery(prepared)

    monkeypatch.setattr(service, "find_artifact", hold_winner)
    actors = [threading.Thread(target=actor, args=(name,)) for name in ("term-a", "term-b")]
    for thread in actors:
        thread.start()
    start.wait()
    assert winner_at_plan.wait(timeout=2)
    for thread in actors:
        thread.join(timeout=4)
    assert all(not thread.is_alive() for thread in actors)
    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], service.DecontaminationError)
    assert "artifact_lease_busy" in str(errors[0])


def test_lease_lost_after_plan_two_actor_barrier_aborts_before_replace(monkeypatch, tmp_path):
    artifact = _install_fixture_home(monkeypatch, tmp_path)
    boundary = threading.Barrier(2)
    lease_removed = threading.Event()
    original_validate = service._validate_artifact_lease
    errors = []

    def validate_after_disruption(lease):
        boundary.wait()
        assert lease_removed.wait(timeout=2)
        try:
            original_validate(lease)
        finally:
            with service._artifact_lease_lock:
                service._artifact_leases.setdefault(lease.session_uuid, lease.generation)

    def scrubber():
        try:
            service.prepare_content_recovery(
                terminal_id="term", lifecycle_generation=1, session_uuid=POSITIVE_UUID,
                invoker="supervisor", caller_mailbox_id=None, caller_terminal_id=None,
                gating_basis="lease-loss", force=False, show=False, use_cpa=False,
                log_dir=tmp_path / "logs",
            )
        except Exception as exc:
            errors.append(exc)

    def lease_disruptor():
        boundary.wait()
        with service._artifact_lease_lock:
            service._artifact_leases.pop(POSITIVE_UUID, None)
        lease_removed.set()

    monkeypatch.setattr(service, "_validate_artifact_lease", validate_after_disruption)
    actors = [threading.Thread(target=scrubber), threading.Thread(target=lease_disruptor)]
    for actor in actors:
        actor.start()
    for actor in actors:
        actor.join(timeout=3)
    assert all(not actor.is_alive() for actor in actors)
    assert len(errors) == 1
    assert isinstance(errors[0], service.DecontaminationError)
    assert "artifact_lease_lost" in str(errors[0])
    assert artifact.read_bytes() == POSITIVE.read_bytes()


def test_symlink_artifact_is_rejected(monkeypatch, tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    target = tmp_path / "real.jsonl"
    shutil.copyfile(POSITIVE, target)
    (sessions / f"rollout-{POSITIVE_UUID}.jsonl").symlink_to(target)
    monkeypatch.setattr(service, "provider_home", lambda _provider: SimpleNamespace(sessions=sessions))
    with pytest.raises(service.DecontaminationError, match="regular_file_required"):
        service.find_artifact(POSITIVE_UUID)


def test_cpa_absence_falls_back_without_error(monkeypatch, tmp_path):
    monkeypatch.delenv("CPA_API_KEY", raising=False)
    records = service.validate_artifact_bytes(
        CONTROL.read_bytes(), CONTROL_UUID, f"rollout-{CONTROL_UUID}.jsonl"
    )
    result = service.discover_cpa_spans(records, config_path=tmp_path / "missing.json")
    assert result.unavailable is True
    assert result.spans == ()


def test_cpa_proposals_are_locally_resolved_and_replacement_is_ignored(monkeypatch, tmp_path):
    records = service.validate_artifact_bytes(
        CONTROL.read_bytes(), CONTROL_UUID, f"rollout-{CONTROL_UUID}.jsonl"
    )
    candidate = service._decoded_candidates(records)[0][2]
    digest = hashlib.sha256(candidate[:1].encode()).hexdigest()
    response = json.dumps({"proposals": [
        {"candidate_index": 0, "start": 0, "end": 1,
         "preimage_sha256": digest, "replacement": "ignored"},
        {"candidate_index": 0, "start": 0, "end": 99,
         "preimage_sha256": digest},
    ]}).encode()

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, _limit): return response

    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"cpa": {"url": "https://example.invalid"}}))
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    monkeypatch.setattr(service.urllib.request, "urlopen", lambda *_a, **_kw: FakeResponse())
    result = service.discover_cpa_spans(records, config_path=config)
    assert len(result.spans) == 1
    assert result.dropped == 1
    assert result.spans[0].replacement_policy == service.NEUTRAL_REPLACEMENT_POLICY


def test_cpa_candidate_caps_force_human_gate():
    candidates = [(1, ("payload", "message"), "x" * 9000)] * 513
    rows, truncated = service._candidate_payload(candidates)
    assert len(rows) <= 512
    assert all(len(row["text"]) <= 8192 for row in rows)
    assert truncated is True


def test_cpa_proposal_overflow_forces_human_gate(monkeypatch, tmp_path):
    records = service.validate_artifact_bytes(
        CONTROL.read_bytes(), CONTROL_UUID, f"rollout-{CONTROL_UUID}.jsonl"
    )
    candidate = service._decoded_candidates(records)[0][2]
    digest = hashlib.sha256(candidate[:1].encode()).hexdigest()
    response = json.dumps({"proposals": [
        {"candidate_index": 0, "start": 0, "end": 1, "preimage_sha256": digest}
        for _ in range(65)
    ]}).encode()

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, _limit): return response

    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"cpa": {"url": "https://example.invalid"}}))
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    monkeypatch.setattr(service.urllib.request, "urlopen", lambda *_a, **_kw: FakeResponse())
    result = service.discover_cpa_spans(records, config_path=config)
    assert len(result.spans) == 64
    assert result.dropped == 1
    assert result.human_gate_required is True


def test_public_summary_discloses_span_detail_only_with_show(monkeypatch, tmp_path):
    _artifact, prepared = _prepare(monkeypatch, tmp_path)
    try:
        hidden = service.public_scrub_summary(prepared, show=False)
        shown = service.public_scrub_summary(prepared, show=True)
        assert "spans" not in hidden
        assert shown["spans"]
        assert all("expected_preimage_sha256" in row for row in shown["spans"])
    finally:
        service.restore_backup(prepared)
        service.release_prepared_recovery(prepared)
