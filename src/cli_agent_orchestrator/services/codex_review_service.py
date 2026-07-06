"""Async ``codex review`` launcher and completion notifier."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from cli_agent_orchestrator.clients.database import create_inbox_message
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services.inbox_service import inbox_service

logger = logging.getLogger(__name__)

VALID_REVIEW_SCOPES = {"uncommitted", "base", "commit"}
STDERR_TAIL_CHARS = 4000
FINDINGS_FILE_GITIGNORE_MESSAGE = "gitignore tmp/ in {repo} or use scope=base/commit"
QUOTA_FAILURE_RE = re.compile(
    r"(?:"
    r"\b(?:HTTP\s*)?(?:402|403|429)\b"
    r"|预扣费"
    r"|\busage limit\b"
    r"|\brate limit exceeded\b"
    r"|\bquota exceeded\b"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CodexReviewJob:
    """A launched or launchable Codex review."""

    review_id: str
    requester_id: str
    instructions: str | None
    scope: str | None
    target: str | None
    cwd: Path
    findings_file: Path
    command: tuple[str, ...]


def build_codex_review_command(
    instructions: str | None = None,
    scope: str | None = None,
    target: str | None = None,
) -> list[str]:
    """Map the MCP scope contract to ``codex review`` flags."""
    has_instructions = bool(instructions and instructions.strip())
    if has_instructions and scope is not None:
        raise ValueError(
            "instructions and scope are mutually exclusive "
            "(codex-cli rejects scope flags combined with a prompt)"
        )
    if not has_instructions and scope is None:
        if target:
            raise ValueError("target requires scope 'base' or 'commit'")
        raise ValueError("instructions or scope is required")

    command = ["codex", "review"]
    if scope is None:
        if target:
            raise ValueError("target requires scope 'base' or 'commit'")
    elif scope == "uncommitted":
        if target:
            raise ValueError("target is not valid with scope 'uncommitted'")
        command.append("--uncommitted")
    elif scope == "base":
        if not target:
            raise ValueError("target is required when scope is 'base'")
        command.extend(["--base", target])
    elif scope == "commit":
        if not target:
            raise ValueError("target is required when scope is 'commit'")
        command.extend(["--commit", target])
    else:
        valid = ", ".join(sorted(VALID_REVIEW_SCOPES))
        raise ValueError(f"invalid scope {scope!r}; expected one of: {valid}")

    if scope is None:
        command.append(instructions.strip() if instructions else "")
    return command


def create_codex_review_job(
    requester_id: str,
    instructions: str | None = None,
    scope: str | None = None,
    target: str | None = None,
    cwd: str | None = None,
) -> CodexReviewJob:
    """Create a review job descriptor without launching the subprocess.

    ``cwd`` is required so callers explicitly choose the repository under
    review. Instructions-only and uncommitted reviews also require the findings
    path to be ignored by Git to avoid reviewing CAO's own output file.
    """
    if cwd is None or not cwd.strip():
        raise ValueError("cwd is required")
    review_id = uuid.uuid4().hex[:8]
    review_cwd = Path(cwd).expanduser()
    findings_file = review_cwd / "tmp" / "orch" / f"review-{review_id}.md"
    command = tuple(build_codex_review_command(instructions, scope, target))
    _validate_findings_file_is_git_ignored(review_cwd, findings_file, scope)
    return CodexReviewJob(
        review_id=review_id,
        requester_id=requester_id,
        instructions=instructions,
        scope=scope,
        target=target,
        cwd=review_cwd,
        findings_file=findings_file,
        command=command,
    )


def _validate_findings_file_is_git_ignored(
    review_cwd: Path,
    findings_file: Path,
    scope: str | None,
) -> None:
    """Ensure generated findings cannot contaminate working-tree review input."""
    if scope in {"base", "commit"}:
        return

    relative_findings = findings_file.relative_to(review_cwd).as_posix()
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", relative_findings],
            cwd=str(review_cwd),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ValueError(
            f"unable to verify findings file ignore status; "
            + FINDINGS_FILE_GITIGNORE_MESSAGE.format(repo=review_cwd)
        ) from exc
    if result.returncode != 0:
        raise ValueError(
            f"findings file {relative_findings!r} is not git-ignored; "
            + FINDINGS_FILE_GITIGNORE_MESSAGE.format(repo=review_cwd)
        )


def _stderr_tail(stderr: str) -> str:
    if len(stderr) <= STDERR_TAIL_CHARS:
        return stderr
    return stderr[-STDERR_TAIL_CHARS:]


def _read_findings_text(findings_file: Path) -> str:
    try:
        return findings_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _count_findings(findings_text: str) -> int:
    """Count actionable findings in Codex review markdown."""
    count = 0
    for line in findings_text.splitlines():
        stripped = line.strip()
        if re.match(r"^(?:[-*]|\d+\.)\s+(?:\[[^\]]+\]\s+)?\S", stripped):
            count += 1
    return count


def _completion_label(exit_code: int, stderr: str, findings_text: str) -> str:
    if exit_code != 0 or not findings_text.strip():
        kind = "quota" if QUOTA_FAILURE_RE.search(stderr) else "infra"
        return f"REVIEW FAILED ({kind})"
    return f"REVIEW COMPLETED ({_count_findings(findings_text)} findings)"


def _completion_message(job: CodexReviewJob, exit_code: int, stderr: str) -> str:
    findings_text = _read_findings_text(job.findings_file)
    label = _completion_label(exit_code, stderr, findings_text)
    lines = [
        f"Codex review {job.review_id}: {label}",
        f"Exit code: {exit_code}",
        f"Findings file: {job.findings_file}",
    ]
    if exit_code != 0 and stderr:
        lines.extend(["", "stderr tail:", _stderr_tail(stderr).rstrip()])
    return "\n".join(lines).rstrip()


def _push_completion(
    job: CodexReviewJob,
    exit_code: int,
    stderr: str,
    registry: PluginRegistry | None = None,
) -> None:
    """Queue a server-generated completion message to the requester."""
    message = _completion_message(job, exit_code, stderr)
    create_inbox_message(f"codex_review:{job.review_id}", job.requester_id, message)
    inbox_service.deliver_pending(job.requester_id, registry=registry)


async def run_codex_review_job(
    job: CodexReviewJob,
    registry: PluginRegistry | None = None,
) -> None:
    """Run ``codex review`` and push completion when it exits."""
    exit_code = -1
    stderr_text = ""
    try:
        if not job.cwd.is_dir():
            raise FileNotFoundError(f"cwd not found or not a directory: {job.cwd}")
        job.findings_file.parent.mkdir(parents=True, exist_ok=True)
        with job.findings_file.open("wb") as stdout_file:
            proc = await asyncio.create_subprocess_exec(
                *job.command,
                cwd=str(job.cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            exit_code = proc.returncode if proc.returncode is not None else -1
            stderr_text = stderr.decode(errors="replace") if stderr else ""
    except Exception as exc:
        stderr_text = str(exc)
        logger.exception("Codex review %s failed before process exit", job.review_id)
    finally:
        try:
            _push_completion(job, exit_code, stderr_text, registry=registry)
        except Exception:
            logger.exception("Failed to push Codex review %s completion", job.review_id)


def start_codex_review(
    requester_id: str,
    instructions: str | None = None,
    scope: str | None = None,
    target: str | None = None,
    cwd: str | None = None,
    registry: PluginRegistry | None = None,
) -> dict[str, Any]:
    """Schedule a Codex review and return the async handle immediately.

    ``cwd`` is mandatory; callers must pass the target repository explicitly.
    """
    job = create_codex_review_job(
        requester_id=requester_id,
        instructions=instructions,
        scope=scope,
        target=target,
        cwd=cwd,
    )
    asyncio.create_task(run_codex_review_job(job, registry=registry))
    return codex_review_response(job)


def codex_review_response(job: CodexReviewJob) -> dict[str, Any]:
    """Public response shape shared by the API and tests."""
    return {
        "success": True,
        "review_id": job.review_id,
        "terminal_id": job.review_id,
        "findings_file": str(job.findings_file),
        "command": list(job.command),
    }


__all__: Sequence[str] = (
    "CodexReviewJob",
    "build_codex_review_command",
    "codex_review_response",
    "create_codex_review_job",
    "run_codex_review_job",
    "start_codex_review",
)
