"""Production entrypoint for the ChatGPT-web findings runner (r3).

``ChatGptWebProvider`` launches this in the worker's tmux pane
(``python -m cli_agent_orchestrator.chatgpt_web_runner``). It prints
``[chatgpt_web] READY`` and then reads ONE task from stdin (the pasted dispatch
body). The task's header names the pinned artifact and the attempt's PULL
context — Amendment D deleted the pushed ``BUNDLE:`` attachment (D10), so the
model reads the reviewed source through the read-only connector instead:

    ARTIFACT:  /abs/path/to/pinned-artifact.md
    WORKTREE:  /abs/path/to/frozen-read-only-worktree   (optional)
    COMMIT:    <reviewed commit>                        (optional)
    BASE:      <base commit>                            (optional)
    SOURCE:    relative/path/inside/the/worktree        (repeatable)
    <blank line>
    <the findings/plain prompt to send>

Each ``SOURCE:`` line adds one path to the allowlisted manifest the attempt's
access token is bound to; a read outside it is refused
``PATH_NOT_IN_MANIFEST`` by the connector, not merely discouraged by the prompt.

It runs the D2 anchor sequence via :func:`production.run_production_review` as the
WORKER (env ``CAO_TERMINAL_ID`` / ``CAO_TERMINAL_TOKEN`` / ``CAO_ENDPOINT`` /
``CAO_CALLBACK_TERMINAL_ID``), prints one terminal status marker
(``FINDINGS-READY`` / ``FINDINGS-INVALID`` / ``ERROR`` / ``WAIT_USER``) plus a
``CONDITION`` marker on a typed condition, and exits.

Status markers are the provider's ``get_status`` contract. NEVER prints a
cookie/bearer/sentinel value.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_LOG_DIR = Path("/data/cao-scratch/worker-scratch/f862-build")


def _marker(text: str) -> None:
    sys.stdout.write(f"[chatgpt_web] {text}\n")
    sys.stdout.flush()


@dataclass(frozen=True)
class ParsedTask:
    """The dispatch header: the pinned artifact plus the attempt's pull context."""

    artifact_path: str
    prompt: str
    source_manifest: tuple[str, ...] = ()
    frozen_worktree: Optional[str] = None
    reviewed_commit: Optional[str] = None
    base_commit: Optional[str] = None


_HEADERS = {
    "ARTIFACT": "artifact",
    "WORKTREE": "worktree",
    "COMMIT": "commit",
    "BASE": "base",
    "SOURCE": "source",
}


def _parse_task(raw: str) -> ParsedTask:
    """Parse the dispatch header. ``BUNDLE:`` is gone with the upload path."""
    fields: dict[str, str] = {}
    sources: list[str] = []
    lines = raw.splitlines()
    body_start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        key = stripped.split(":", 1)[0].strip().upper() if ":" in stripped else ""
        if key in _HEADERS:
            value = stripped.split(":", 1)[1].strip()
            if key == "SOURCE":
                if value:
                    sources.append(value)
            elif value:
                fields[_HEADERS[key]] = value
            continue
        if stripped == "":
            body_start = index + 1
            break
        body_start = index
        break
    prompt = "\n".join(lines[body_start:]).strip()
    return ParsedTask(
        artifact_path=fields.get("artifact", ""),
        prompt=prompt,
        source_manifest=tuple(sources),
        frozen_worktree=fields.get("worktree"),
        reviewed_commit=fields.get("commit"),
        base_commit=fields.get("base"),
    )


def main() -> int:
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.DEBUG,
            filename=str(_LOG_DIR / "production-run.log"),
            filemode="a",
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    except Exception:
        pass

    task_file: Optional[str] = None
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--task-file" and i + 1 < len(argv):
            task_file = argv[i + 1]

    _marker("READY")

    if task_file:
        raw = _await_task_file(task_file)
    else:
        raw = sys.stdin.read()

    if not raw.strip():
        _marker("ERROR no_task")
        return 1

    task = _parse_task(raw)
    if not task.artifact_path or not task.prompt:
        _marker("ERROR malformed_task")
        return 1

    _marker("RUNNING")
    from cli_agent_orchestrator.chatgpt_web_runner.errors import RunnerError
    from cli_agent_orchestrator.chatgpt_web_runner.production import run_production_review

    try:
        outcome = run_production_review(
            task_text=task.prompt,
            artifact_path=task.artifact_path,
            source_manifest=list(task.source_manifest),
            frozen_worktree=task.frozen_worktree,
            reviewed_commit=task.reviewed_commit,
            base_commit=task.base_commit,
        )
    except RunnerError as exc:
        _marker(f"CONDITION {exc.code.value}")
        _marker(f"ERROR {exc.code.value}")
        return 1
    except Exception as exc:  # never leave the pane without a terminal marker
        logging.exception("production run crashed")
        _marker(f"ERROR {type(exc).__name__}")
        return 1

    if outcome.ok:
        _marker(f"FINDINGS-READY {outcome.report_path} sha256={outcome.report_body_sha256}")
        return 0
    code = outcome.error_code.value if outcome.error_code else "none"
    if code == "report_invalid":
        _marker(f"FINDINGS-INVALID {code}")
    else:
        _marker(f"ERROR {code} delivery={outcome.delivery_state.value}")
    return 1


def _await_task_file(path: str, *, timeout_s: float = 900.0, poll_s: float = 1.0) -> str:
    """Poll for the per-worker task file the provider writes, then read+consume it."""
    import time

    p = Path(path)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if p.exists():
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                text = ""
            if text.strip():
                try:
                    p.unlink()  # consume: one task per launch
                except OSError:
                    pass
                return text
        time.sleep(poll_s)
    return ""


if __name__ == "__main__":
    sys.exit(main())
