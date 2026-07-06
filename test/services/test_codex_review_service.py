import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.services import codex_review_service as svc


def test_build_codex_review_command_maps_scopes():
    assert svc.build_codex_review_command("focus on X") == ["codex", "review", "focus on X"]
    assert svc.build_codex_review_command(scope="uncommitted") == [
        "codex",
        "review",
        "--uncommitted",
    ]
    assert svc.build_codex_review_command(scope="base", target="main") == [
        "codex",
        "review",
        "--base",
        "main",
    ]
    assert svc.build_codex_review_command(scope="commit", target="abc123") == [
        "codex",
        "review",
        "--commit",
        "abc123",
    ]


@pytest.mark.parametrize(
    ("instructions", "scope", "target", "message"),
    [
        (None, None, None, "instructions or scope is required"),
        ("", None, None, "instructions or scope is required"),
        ("   ", None, None, "instructions or scope is required"),
        ("focus", "uncommitted", None, "instructions and scope are mutually exclusive"),
        ("focus", "base", "main", "instructions and scope are mutually exclusive"),
        (None, "bogus", None, "invalid scope"),
        (None, "base", None, "target is required"),
        (None, "commit", None, "target is required"),
        ("focus", None, "main", "target requires scope"),
        (None, "uncommitted", "main", "target is not valid"),
    ],
)
def test_build_codex_review_command_rejects_invalid_contract(instructions, scope, target, message):
    with pytest.raises(ValueError, match=message):
        svc.build_codex_review_command(instructions, scope, target)


def test_create_codex_review_job_requires_cwd():
    with pytest.raises(ValueError, match="cwd is required"):
        svc.create_codex_review_job(requester_id="deadbeef", scope="commit", target="abc123")


@pytest.mark.parametrize(
    ("instructions", "scope"),
    [
        ("focus", None),
        (None, "uncommitted"),
    ],
)
def test_create_codex_review_job_checks_findings_file_is_git_ignored(
    tmp_path, monkeypatch, instructions, scope
):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(svc.subprocess, "run", fake_run)

    job = svc.create_codex_review_job(
        requester_id="deadbeef",
        instructions=instructions,
        scope=scope,
        cwd=str(tmp_path),
    )

    assert calls == [
        (
            ["git", "check-ignore", "-q", f"tmp/orch/review-{job.review_id}.md"],
            {
                "cwd": str(tmp_path),
                "check": False,
                "stdout": svc.subprocess.DEVNULL,
                "stderr": svc.subprocess.DEVNULL,
            },
        )
    ]


@pytest.mark.parametrize(
    ("scope", "target"),
    [
        ("base", "main"),
        ("commit", "abc123"),
    ],
)
def test_create_codex_review_job_skips_ignore_check_for_fixed_scopes(
    tmp_path, monkeypatch, scope, target
):
    monkeypatch.setattr(
        svc.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not check git")),
    )

    job = svc.create_codex_review_job(
        requester_id="deadbeef",
        scope=scope,
        target=target,
        cwd=str(tmp_path),
    )

    assert job.scope == scope


def test_create_codex_review_job_rejects_unignored_findings_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        svc.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )

    with pytest.raises(ValueError, match=r"gitignore tmp/ in .* or use scope=base/commit"):
        svc.create_codex_review_job(
            requester_id="deadbeef",
            scope="uncommitted",
            cwd=str(tmp_path),
        )


@pytest.mark.asyncio
async def test_run_codex_review_job_creates_findings_path_and_pushes_completion(
    tmp_path, monkeypatch
):
    job = svc.CodexReviewJob(
        review_id="abc123ef",
        requester_id="deadbeef",
        instructions=None,
        scope="uncommitted",
        target=None,
        cwd=tmp_path,
        findings_file=tmp_path / "tmp" / "orch" / "review-abc123ef.md",
        command=("codex", "review", "--uncommitted"),
    )
    created_messages = []
    delivered = []

    class Proc:
        returncode = 0

        async def communicate(self):
            return None, b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        assert args == job.command
        assert kwargs["cwd"] == str(tmp_path)
        kwargs["stdout"].write(b"review body\n")
        kwargs["stdout"].flush()
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        svc,
        "create_inbox_message",
        lambda sender, receiver, message: created_messages.append((sender, receiver, message)),
    )
    monkeypatch.setattr(
        svc.inbox_service,
        "deliver_pending",
        lambda receiver, registry=None: delivered.append((receiver, registry)),
    )

    await svc.run_codex_review_job(job)

    assert job.findings_file.read_text() == "review body\n"
    assert created_messages == [
        (
            "codex_review:abc123ef",
            "deadbeef",
            (
                "Codex review abc123ef: REVIEW COMPLETED (0 findings)\n"
                "Exit code: 0\n"
                f"Findings file: {job.findings_file}"
            ),
        )
    ]
    assert delivered == [("deadbeef", None)]


@pytest.mark.asyncio
async def test_run_codex_review_job_pushes_stderr_tail_on_nonzero(tmp_path, monkeypatch):
    job = svc.CodexReviewJob(
        review_id="badc0dex",
        requester_id="deadbeef",
        instructions="focus",
        scope=None,
        target=None,
        cwd=tmp_path,
        findings_file=tmp_path / "tmp" / "orch" / "review-badc0dex.md",
        command=("codex", "review", "focus"),
    )
    created_messages = []

    class Proc:
        returncode = 2

        async def communicate(self):
            return None, b"quota exceeded\n"

    async def fake_create_subprocess_exec(*args, **kwargs):
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        svc,
        "create_inbox_message",
        lambda sender, receiver, message: created_messages.append((sender, receiver, message)),
    )
    monkeypatch.setattr(svc.inbox_service, "deliver_pending", lambda receiver, registry=None: None)

    await svc.run_codex_review_job(job)

    message = created_messages[0][2]
    assert "REVIEW FAILED (quota)" in message
    assert "Exit code: 2" in message
    assert f"Findings file: {job.findings_file}" in message
    assert "stderr tail:\nquota exceeded" in message


@pytest.mark.asyncio
async def test_run_codex_review_job_pushes_failure_for_bad_cwd(tmp_path, monkeypatch):
    missing_cwd = tmp_path / "missing"
    job = svc.CodexReviewJob(
        review_id="badc0cwd",
        requester_id="deadbeef",
        instructions="focus",
        scope=None,
        target=None,
        cwd=missing_cwd,
        findings_file=missing_cwd / "tmp" / "orch" / "review-badc0cwd.md",
        command=("codex", "review", "focus"),
    )
    created_messages = []

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess should not launch for bad cwd")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_if_called)
    monkeypatch.setattr(
        svc,
        "create_inbox_message",
        lambda sender, receiver, message: created_messages.append((sender, receiver, message)),
    )
    monkeypatch.setattr(svc.inbox_service, "deliver_pending", lambda receiver, registry=None: None)

    await svc.run_codex_review_job(job)

    assert not missing_cwd.exists()
    message = created_messages[0][2]
    assert "REVIEW FAILED (infra)" in message
    assert "Exit code: -1" in message
    assert f"Findings file: {job.findings_file}" in message
    assert f"stderr tail:\ncwd not found or not a directory: {missing_cwd}" in message


def test_completion_label_parses_success_finding_count(tmp_path):
    findings = tmp_path / "findings.md"
    findings.write_text(
        "- [P1] first issue\n"
        "context line\n"
        "1. [P2] second issue\n"
        "No bullet here\n",
        encoding="utf-8",
    )
    job = svc.CodexReviewJob(
        review_id="count1234",
        requester_id="deadbeef",
        instructions=None,
        scope="uncommitted",
        target=None,
        cwd=tmp_path,
        findings_file=findings,
        command=("codex", "review", "--uncommitted"),
    )

    message = svc._completion_message(job, exit_code=0, stderr="")

    assert "REVIEW COMPLETED (2 findings)" in message


def test_completion_label_empty_findings_file_is_infra_failure(tmp_path):
    findings = tmp_path / "findings.md"
    findings.write_text("", encoding="utf-8")
    job = svc.CodexReviewJob(
        review_id="empty123",
        requester_id="deadbeef",
        instructions=None,
        scope="uncommitted",
        target=None,
        cwd=tmp_path,
        findings_file=findings,
        command=("codex", "review", "--uncommitted"),
    )

    message = svc._completion_message(job, exit_code=0, stderr="")

    assert "REVIEW FAILED (infra)" in message


def test_completion_label_detects_quota_patterns_on_failure(tmp_path):
    findings = tmp_path / "findings.md"
    findings.write_text("partial output\n", encoding="utf-8")
    job = svc.CodexReviewJob(
        review_id="quota123",
        requester_id="deadbeef",
        instructions=None,
        scope="uncommitted",
        target=None,
        cwd=tmp_path,
        findings_file=findings,
        command=("codex", "review", "--uncommitted"),
    )

    message = svc._completion_message(job, exit_code=1, stderr="403 insufficient quota")

    assert "REVIEW FAILED (quota)" in message


def test_completion_label_treats_insufficient_permissions_as_infra(tmp_path):
    findings = tmp_path / "findings.md"
    findings.write_text("partial output\n", encoding="utf-8")
    job = svc.CodexReviewJob(
        review_id="infra123",
        requester_id="deadbeef",
        instructions=None,
        scope="uncommitted",
        target=None,
        cwd=tmp_path,
        findings_file=findings,
        command=("codex", "review", "--uncommitted"),
    )

    message = svc._completion_message(job, exit_code=1, stderr="insufficient permissions")

    assert "REVIEW FAILED (infra)" in message


def test_codex_review_response_contains_async_handle(tmp_path):
    job = svc.create_codex_review_job(
        requester_id="deadbeef",
        scope="commit",
        target="abc123",
        cwd=str(tmp_path),
    )

    response = svc.codex_review_response(job)

    assert response["success"] is True
    assert response["review_id"] == job.review_id
    assert response["terminal_id"] == job.review_id
    assert response["findings_file"] == str(
        Path(tmp_path) / "tmp" / "orch" / f"review-{job.review_id}.md"
    )
    assert response["command"] == ["codex", "review", "--commit", "abc123"]
