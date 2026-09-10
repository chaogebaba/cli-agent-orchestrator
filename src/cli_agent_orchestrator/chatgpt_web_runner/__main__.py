"""Production entrypoint for the ChatGPT-web findings runner (r3).

``ChatGptWebProvider`` launches this in the worker's tmux pane
(``python -m cli_agent_orchestrator.chatgpt_web_runner``). It prints
``[chatgpt_web] READY`` and then reads ONE task from stdin (the pasted dispatch
body). The task's first line names the pinned artifact and, optionally, a bundle
file to attach:

    ARTIFACT: /abs/path/to/pinned-artifact.md
    BUNDLE: /abs/path/to/bundle.txt        (optional — attach + design_findings)
    <blank line>
    <the findings/plain prompt to send>

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
from pathlib import Path
from typing import Optional

_LOG_DIR = Path("/data/cao-scratch/worker-scratch/f862-build")


def _marker(text: str) -> None:
    sys.stdout.write(f"[chatgpt_web] {text}\n")
    sys.stdout.flush()


def _parse_task(raw: str) -> tuple[str, Optional[str], str]:
    """Return (artifact_path, bundle_path|None, prompt) from the task body."""
    artifact = ""
    bundle: Optional[str] = None
    lines = raw.splitlines()
    body_start = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if s.upper().startswith("ARTIFACT:"):
            artifact = s.split(":", 1)[1].strip()
        elif s.upper().startswith("BUNDLE:"):
            bundle = s.split(":", 1)[1].strip() or None
        elif s == "":
            body_start = i + 1
            break
        else:
            body_start = i
            break
    prompt = "\n".join(lines[body_start:]).strip()
    return artifact, bundle, prompt


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

    artifact, bundle, prompt = _parse_task(raw)
    if not artifact or not prompt:
        _marker("ERROR malformed_task")
        return 1

    _marker("RUNNING")
    from cli_agent_orchestrator.chatgpt_web_runner.errors import RunnerError
    from cli_agent_orchestrator.chatgpt_web_runner.production import run_production_review

    try:
        outcome = run_production_review(
            task_text=prompt, artifact_path=artifact, bundle_path=bundle
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
