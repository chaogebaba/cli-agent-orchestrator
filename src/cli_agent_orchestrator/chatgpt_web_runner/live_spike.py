"""F862 (#718) live-run harness — real end-to-end runs on the user's profile.

Proves the Python runner end-to-end against the real chatgpt.com web session and
supports the three R2 run shapes:

    plain       — a plain findings prompt, no attachment.
    attach      — a prompt PLUS a ~text-file attachment via input#upload-files;
                  the answer must quote the attached bytes.
    findings    — the design_findings position's real shape: the F862 blueprint
                  uploaded as context + a findings prompt -> parsed findings.

Usage:
    uv run python -m cli_agent_orchestrator.chatgpt_web_runner.live_spike \
        <plain|attach|findings> [attach_path]

It NEVER uses the OpenAI API and never prints a cookie/bearer/sentinel value.
Import-safe without a browser.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.chatgpt_web_runner.errors import DeliveryState
from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import SEL_COMPOSER, Transport
from cli_agent_orchestrator.chatgpt_web_runner.output import RunnerOutcome, Submitted
from cli_agent_orchestrator.chatgpt_web_runner.publication import build_report
from cli_agent_orchestrator.chatgpt_web_runner.runtime import (
    CHATGPT_URL,
    build_launch_options,
    launch,
    pin_fingerprint_seed,
    resolve_profile_dir,
)
from cli_agent_orchestrator.chatgpt_web_runner.snapshot_upload import sha256_bytes, sha256_text
from cli_agent_orchestrator.chatgpt_web_runner.submit_ids import new_run_id

_SCRATCH = Path("/data/cao-scratch/worker-scratch/f862-build")
_SCRATCH.mkdir(parents=True, exist_ok=True)

# A tiny snippet for the plain run.
_PLAIN_SNIPPET = "def add(a, b):\n    return a - b   # BUG: subtracts instead of adds\n"


def _plain_prompt(run_id: str, bundle_sha: str) -> str:
    return (
        "You are a findings-only reviewer. Read the snippet and report candidate "
        "findings only — do NOT emit any ruling/verdict.\n\n"
        "```python\n" + _PLAIN_SNIPPET + "```\n\n"
        "List each finding as 'Finding N: <cite> <consequence> <fix>'. "
        f"End with EXACTLY one terminal line and nothing after it: END_REVIEW:{run_id}:{bundle_sha}\n"
    )


def _attach_prompt(run_id: str, bundle_sha: str, canary: str) -> str:
    return (
        "A UTF-8 text file is attached. Answer ONLY from it. "
        f"Quote verbatim the line containing the token {canary}, then state the "
        "attached file's total line count.\n"
        "Report as findings-style lines; do NOT emit any ruling/verdict. "
        f"End with EXACTLY one terminal line and nothing after it: END_REVIEW:{run_id}:{bundle_sha}\n"
    )


def _findings_prompt(run_id: str, bundle_sha: str) -> str:
    return (
        "The attached file is a design blueprint (F862 ChatGPT-web findings lane). "
        "You are a findings-only reviewer for the design_findings position. Read it "
        "and report candidate findings ONLY — structural gaps, under-specified "
        "decisions, or internal contradictions. For each: 'Finding N: <section/decision "
        "cite> <consequence> <proposed amendment>'. Do NOT emit any ruling, verdict, "
        "GATE-YES or GATE-NO. "
        f"End with EXACTLY one terminal line and nothing after it: END_REVIEW:{run_id}:{bundle_sha}\n"
    )


def _build_attach_bundle() -> Path:
    """Build a ~50 KB UTF-8 text file with a canary near the end (run b)."""
    _SCRATCH.mkdir(parents=True, exist_ok=True)
    canary = "CANARY-f862r2-50k"
    lines = [f"line {i:05d}: the quick brown fox jumps over the lazy dog" for i in range(900)]
    # Insert the canary at ~90% depth on its own line.
    insert_at = int(len(lines) * 0.9)
    lines.insert(insert_at, canary)
    text = "\n".join(lines) + "\n"
    p = _SCRATCH / "f862-r2-attach-50k.txt"
    p.write_text(text, encoding="utf-8")
    return p


def _latest_user_msg_id(conv: dict[str, object]) -> str:
    """Return the message id of the newest USER node (our just-submitted turn)."""
    mapping_obj = conv.get("mapping") or {}
    mapping = mapping_obj if isinstance(mapping_obj, dict) else {}
    best = ""
    best_ct = -1.0
    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        msg = node.get("message")
        if not isinstance(msg, dict):
            continue
        author = msg.get("author") or {}
        if isinstance(author, dict) and author.get("role") == "user":
            ct = float(msg.get("create_time") or 0.0)
            if ct >= best_ct:
                best_ct = ct
                best = str(msg.get("id") or "")
    return best


async def _run(mode: str, attach_path: Optional[str]) -> RunnerOutcome:
    started = time.monotonic()
    run_id = new_run_id()

    attach: Optional[Path] = None
    canary = ""
    if mode == "plain":
        bundle_sha = sha256_text(_PLAIN_SNIPPET)
        prompt = _plain_prompt(run_id, bundle_sha)
    elif mode == "attach":
        attach = Path(attach_path) if attach_path else _build_attach_bundle()
        data = attach.read_bytes()
        bundle_sha = sha256_bytes(data)
        canary = "CANARY-f862r2-50k"
        prompt = _attach_prompt(run_id, bundle_sha, canary)
    elif mode == "findings":
        src = Path(
            attach_path
            or "/home/chao/VScode_projects/cli-subagents/orchestrator/blueprints/f862-chatgpt-web-findings-lane.md"
        )
        # The design_findings bundle is a curated TEXT bundle (D8: one text
        # file). Upload it as .txt — that is the file type the upload-probe
        # calibrated the two readiness signals against (a .md chip shows no
        # "Document" label; the label is file-type dependent — F862 r2 finding).
        bundle_txt = _SCRATCH / "f862-blueprint-bundle.txt"
        bundle_txt.write_bytes(src.read_bytes())
        attach = bundle_txt
        data = attach.read_bytes()
        bundle_sha = sha256_bytes(data)
        prompt = _findings_prompt(run_id, bundle_sha)
    else:
        raise SystemExit(f"unknown mode {mode!r} (plain|attach|findings)")

    profile = resolve_profile_dir()
    seed = pin_fingerprint_seed(profile)
    opts = build_launch_options(profile, seed)
    context = await launch(opts)
    page = context.pages[0] if context.pages else await context.new_page()
    transport = Transport(page)
    transport.arm_send_observer()

    await page.goto(CHATGPT_URL, wait_until="domcontentloaded")
    try:
        await page.locator(SEL_COMPOSER).first.wait_for(state="visible", timeout=20000)
    except Exception:
        await context.close()
        return RunnerOutcome(
            ok=False,
            hint="composer not visible — not logged in?",
            delivery_state=DeliveryState.NOTHING_SENT,
            submitted=Submitted.FALSE,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    attach_identity = None
    if attach is not None:
        attach_identity = await transport.attach_file(str(attach))

    await transport.type_prompt(prompt)
    submit = await transport.submit_and_confirm(timeout_s=40.0)
    conv_id = submit.conversation_id
    if submit.delivery_state is not DeliveryState.DELIVERED or not conv_id:
        await context.close()
        return RunnerOutcome(
            ok=False,
            hint=f"delivery={submit.delivery_state.value}",
            delivery_state=submit.delivery_state,
            submitted=Submitted.UNKNOWN,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            bundle_sha256=bundle_sha,
        )

    # Resolve the submitted user-turn id from the first successful GET.
    submitted_user_msg_id = ""
    for _ in range(10):
        probe = await transport.read_conversation(conv_id)
        body = probe.get("body")
        if probe.get("ok") and isinstance(body, dict):
            submitted_user_msg_id = _latest_user_msg_id(body)
            if submitted_user_msg_id:
                break
        await page.wait_for_timeout(1500)

    answer = await transport.poll_to_gate(
        conv_id, submitted_user_msg_id, run_id, bundle_sha, timeout_s=420.0
    )
    report = build_report(
        body_markdown=answer.text,
        artifact_path=str(attach) if attach else "(plain snippet)",
        artifact_sha256=bundle_sha,
        bundle_sha256=bundle_sha,
        model_slug=answer.model_slug,
        thinking_effort=answer.thinking_effort,
        run_id=run_id,
    )
    _SCRATCH.mkdir(parents=True, exist_ok=True)
    out_path = _SCRATCH / f"live-run-{mode}-report.md"
    out_path.write_text(report.body, encoding="utf-8")

    await context.close()
    return RunnerOutcome(
        ok=True,
        delivery_state=DeliveryState.DELIVERED,
        submitted=Submitted.TRUE,
        conversation_url=f"https://chatgpt.com/c/{conv_id}",
        elapsed_ms=int((time.monotonic() - started) * 1000),
        model_slug=answer.model_slug,
        thinking_effort=answer.thinking_effort,
        bundle_sha256=bundle_sha,
        answer=answer.text,
        report_path=str(out_path),
        report_body_sha256=report.body_sha256,
        attachment_identity=(
            {
                "file_sha256": attach_identity.file_sha256,
                "byte_length": attach_identity.byte_length,
                "line_count": attach_identity.line_count,
                "submitted_filename": attach_identity.submitted_filename,
            }
            if attach_identity
            else None
        ),
    )


def main() -> int:
    import logging

    logging.basicConfig(
        level=logging.DEBUG,
        filename=str(_SCRATCH / "live-run.log"),
        filemode="a",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    mode = sys.argv[1] if len(sys.argv) > 1 else "plain"
    attach_path = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        outcome = asyncio.run(_run(mode, attach_path))
    except Exception as exc:  # never lose the run to an uncaught error
        logging.exception("live run crashed")
        print(json.dumps({"ok": False, "crash": f"{type(exc).__name__}: {exc}"}))
        return 1
    env = outcome.to_envelope()
    if env.get("answer"):
        env["answer"] = env["answer"][:1200]
    print(json.dumps(env, indent=2, sort_keys=True))
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    sys.exit(main())
