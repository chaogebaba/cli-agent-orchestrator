from pathlib import Path

from cli_agent_orchestrator.services import base_digest_service as service
from cli_agent_orchestrator.services.fork_context_service import (
    SnapshotDelta,
    SnapshotEntry,
)


def _delta(*entries: SnapshotEntry) -> SnapshotDelta:
    return SnapshotDelta("head", tuple(entries))


def test_path_codec_keeps_newline_and_literal_backslash_n_distinct():
    literal = "line\\n"
    newline = "line\n"
    assert service.encode_path(literal) != service.encode_path(newline)
    assert service.decode_path(service.encode_path(literal)) == literal
    assert service.decode_path(service.encode_path(newline)) == newline


def test_publish_and_evaluate_covered(tmp_path: Path):
    delta = _delta(SnapshotEntry("a:b", "sha256", "a" * 64))
    row = {"name": "base", "cwd": str(tmp_path), "digest_head": None}
    artifact = service.publish(
        base="base", cwd=str(tmp_path), parent_artifact_sha=None,
        delta=delta, body="general orientation\n", round_number=1,
    )
    result = service.evaluate(row, delta)
    assert isinstance(result, service.DigestCovered)
    assert result.artifact.artifact_sha == artifact.artifact_sha
    assert artifact.path.stat().st_size <= service.MAX_DIGEST_BYTES


def test_no_artifact_is_pending_and_uncovered_is_pending(tmp_path: Path):
    delta = _delta(SnapshotEntry("a", "absent"))
    row = {"name": "base", "cwd": str(tmp_path), "digest_head": None}
    assert isinstance(service.evaluate(row, delta), service.DigestPending)
    service.publish(
        base="base", cwd=str(tmp_path), parent_artifact_sha=None,
        delta=delta, body="ctx", round_number=1,
    )
    assert isinstance(
        service.evaluate(row, _delta(SnapshotEntry("b", "absent"))),
        service.DigestPending,
    )


def test_lineage_mismatch_and_acquisition_failure_are_invalid(tmp_path: Path):
    delta = _delta(SnapshotEntry("a", "sha256", "b" * 64))
    service.publish(
        base="base", cwd=str(tmp_path), parent_artifact_sha=None,
        delta=delta, body="ctx", round_number=1,
    )
    row = {"name": "base", "cwd": str(tmp_path), "digest_head": "c" * 64}
    result = service.evaluate(row, delta)
    assert isinstance(result, service.DigestInvalid)
    assert result.reason == "lineage"
    failure = SnapshotDelta(None, acquisition_error="non-utf8-path")
    result = service.evaluate(row, failure)
    assert isinstance(result, service.DigestInvalid)
    assert result.reason == "acquisition:non-utf8-path"


def test_publish_rejects_over_budget(tmp_path: Path):
    delta = _delta(SnapshotEntry("a", "absent"))
    try:
        service.publish(
            base="base", cwd=str(tmp_path), parent_artifact_sha=None,
            delta=delta, body="x" * service.MAX_DIGEST_BYTES, round_number=1,
        )
    except ValueError as exc:
        assert str(exc) == "over-budget"
    else:
        raise AssertionError("over-budget digest was published")
