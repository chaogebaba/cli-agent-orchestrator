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
        base="base",
        cwd=str(tmp_path),
        parent_artifact_sha=None,
        delta=delta,
        body="general orientation\n",
        round_number=1,
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
        base="base",
        cwd=str(tmp_path),
        parent_artifact_sha=None,
        delta=delta,
        body="ctx",
        round_number=1,
    )
    assert isinstance(
        service.evaluate(row, _delta(SnapshotEntry("b", "absent"))),
        service.DigestPending,
    )


def test_lineage_mismatch_and_acquisition_failure_are_invalid(tmp_path: Path):
    delta = _delta(SnapshotEntry("a", "sha256", "b" * 64))
    service.publish(
        base="base",
        cwd=str(tmp_path),
        parent_artifact_sha=None,
        delta=delta,
        body="ctx",
        round_number=1,
    )
    row = {"name": "base", "cwd": str(tmp_path), "digest_head": "c" * 64}
    result = service.evaluate(row, delta)
    assert isinstance(result, service.DigestInvalid)
    assert result.reason == "lineage"
    failure = SnapshotDelta(None, acquisition_error="non-utf8-path")
    result = service.evaluate(row, failure)
    assert isinstance(result, service.DigestUnobservable)
    assert result.reason == "apparatus_unavailable"
    assert result.detail == "non-utf8-path"


def test_publish_rejects_over_budget(tmp_path: Path):
    delta = _delta(
        SnapshotEntry("a", "absent"),
        SnapshotEntry("bb", "absent"),
        SnapshotEntry("ccc", "absent"),
        SnapshotEntry("dddd", "absent"),
        SnapshotEntry("eeeee", "absent"),
        SnapshotEntry("ffffff", "absent"),
    )
    try:
        service.publish(
            base="base",
            cwd=str(tmp_path),
            parent_artifact_sha=None,
            delta=delta,
            body="x" * service.MAX_DIGEST_BYTES,
            round_number=1,
        )
    except service.OverBudgetError as exc:
        assert exc.kind == "rendered"
        assert exc.detail.manifest_bytes is not None
        assert exc.detail.body_bytes == service.MAX_DIGEST_BYTES + 1
        assert exc.detail.cap == service.MAX_DIGEST_BYTES
        assert exc.detail.top_dirty == tuple(
            (path, len(f"absent - {path}")) for path in ("ffffff", "eeeee", "dddd", "ccc", "bb")
        )
    else:
        raise AssertionError("over-budget digest was published")


def test_ac10_on_disk_and_rendered_budget_failures_are_distinct(tmp_path: Path):
    directory = tmp_path / "tmp" / "orch" / "digests"
    directory.mkdir(parents=True)
    candidate = directory / "base-2026-07-27-r1.md"
    candidate.write_bytes(b"x" * (service.MAX_DIGEST_BYTES + 7))
    delta = _delta(SnapshotEntry("a", "absent"))

    result = service.evaluate({"name": "base", "cwd": str(tmp_path), "digest_head": None}, delta)

    assert isinstance(result, service.DigestInvalid)
    assert result.reason == "over_budget_artifact"
    assert isinstance(result.detail, service.BudgetBreakdown)
    assert result.detail.artifact_bytes == service.MAX_DIGEST_BYTES + 7
    assert result.detail.cap == service.MAX_DIGEST_BYTES


def test_f78_over_budget_artifact_has_manifest_body_breakdown(tmp_path: Path):
    """F78: when on-disk artifact exceeds budget, BudgetBreakdown splits manifest vs body."""
    directory = tmp_path / "tmp" / "orch" / "digests"
    directory.mkdir(parents=True)
    candidate = directory / "base-2026-08-07-r1.md"

    # Write a well-structured artifact that exceeds the budget
    manifest_section = (
        b"<!-- digest-manifest\n"
        b"base: base\n"
        b"parent_artifact_sha: genesis\n"
        b"artifact_sha: " + b"a" * 64 + b"\n"
        b"sha256 " + b"b" * 64 + b" big/path/one.py\n"
        b"sha256 " + b"c" * 64 + b" big/path/two.py\n"
        b"-->\n"
    )
    body_section = b"x" * (service.MAX_DIGEST_BYTES - len(manifest_section) + 100)
    candidate.write_bytes(manifest_section + body_section)

    delta = _delta(SnapshotEntry("a", "absent"))
    result = service.evaluate({"name": "base", "cwd": str(tmp_path), "digest_head": None}, delta)

    assert isinstance(result, service.DigestInvalid)
    assert result.reason == "over_budget_artifact"
    assert isinstance(result.detail, service.BudgetBreakdown)
    assert result.detail.manifest_bytes == len(manifest_section)
    assert result.detail.body_bytes == len(body_section)
    assert result.detail.artifact_bytes == len(manifest_section) + len(body_section)
    assert result.detail.cap == service.MAX_DIGEST_BYTES
    assert len(result.detail.top_dirty) == 2


def test_f78_over_budget_no_manifest_structure_still_returns_breakdown(tmp_path: Path):
    """F78: when artifact is not parseable, manifest/body are None but breakdown is still returned."""
    directory = tmp_path / "tmp" / "orch" / "digests"
    directory.mkdir(parents=True)
    candidate = directory / "base-2026-08-07-r1.md"
    candidate.write_bytes(b"garbage" * 5000)

    delta = _delta(SnapshotEntry("a", "absent"))
    result = service.evaluate({"name": "base", "cwd": str(tmp_path), "digest_head": None}, delta)

    assert isinstance(result, service.DigestInvalid)
    assert result.reason == "over_budget_artifact"
    assert isinstance(result.detail, service.BudgetBreakdown)
    assert result.detail.manifest_bytes is None
    assert result.detail.body_bytes is None


def test_f79_unhashable_entry_includes_detail_with_paths(tmp_path: Path):
    """F79: DigestUnobservable for unhashable entries names the paths in detail."""
    delta = _delta(
        SnapshotEntry("good.py", "sha256", "a" * 64),
        SnapshotEntry("bad.py", "unhashable"),
        SnapshotEntry("also-bad.py", "unhashable"),
    )
    # First publish a valid artifact that matches the same entries
    artifact = service.publish(
        base="base",
        cwd=str(tmp_path),
        parent_artifact_sha=None,
        delta=_delta(
            SnapshotEntry("good.py", "sha256", "a" * 64),
            SnapshotEntry("bad.py", "unhashable"),
            SnapshotEntry("also-bad.py", "unhashable"),
        ),
        body="ctx",
        round_number=1,
    )
    row = {"name": "base", "cwd": str(tmp_path), "digest_head": None}
    result = service.evaluate(row, delta)

    assert isinstance(result, service.DigestUnobservable)
    assert result.reason == "unhashable_entry"
    assert result.detail is not None
    assert "bad.py" in result.detail
    assert "also-bad.py" in result.detail
