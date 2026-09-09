"""F862 (#718) live-spike harness — ONE real send on the user's profile.

Proves the Python runner end-to-end against the real chatgpt.com web session:
launch (pinned seed, headful, humanize=false) -> navigate -> type an inlined
findings prompt -> submit (four-way confirm) -> poll the conversation GET to the
binary gate -> validate model/effort/sentinel -> build the FINDINGS-READY report
-> print the envelope (secrets stripped). It NEVER uses the OpenAI API and never
prints a cookie/bearer/sentinel value.

This is a spike/acceptance harness, not the production dispatch entrypoint (the
provider drives production). It is import-safe without a browser; run it with
``uv run python -m cli_agent_orchestrator.chatgpt_web_runner.live_spike``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from cli_agent_orchestrator.chatgpt_web_runner.errors import DeliveryState
from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import (
    SEL_COMPOSER,
    Transport,
)
from cli_agent_orchestrator.chatgpt_web_runner.output import RunnerOutcome, Submitted
from cli_agent_orchestrator.chatgpt_web_runner.poll_gate import evaluate_gate  # noqa: F401
from cli_agent_orchestrator.chatgpt_web_runner.publication import build_report
from cli_agent_orchestrator.chatgpt_web_runner.runtime import (
    CHATGPT_URL,
    build_launch_options,
    launch,
    pin_fingerprint_seed,
    resolve_profile_dir,
)
from cli_agent_orchestrator.chatgpt_web_runner.snapshot_upload import sha256_text
from cli_agent_orchestrator.chatgpt_web_runner.submit_ids import new_run_id

# A tiny pinned snippet (the "bundle") the model reviews. Kept trivial so the
# spike proves the transport, not the model's reasoning.
_SNIPPET = "def add(a, b):\n" "    return a - b   # BUG: subtracts instead of adds\n"


def _prompt(run_id: str, bundle_sha: str) -> str:
    return (
        "You are a findings-only reviewer. Read the snippet and report candidate "
        "findings only — do NOT emit any ruling/verdict.\n\n"
        "```python\n" + _SNIPPET + "```\n\n"
        "List each finding as 'Finding N: <cite> <consequence> <fix>'. "
        f"End with EXACTLY one terminal line and nothing after it: "
        f"END_REVIEW:{run_id}:{bundle_sha}\n"
    )


async def _run() -> RunnerOutcome:
    started = time.monotonic()
    run_id = new_run_id()
    bundle_sha = sha256_text(_SNIPPET)
    prompt = _prompt(run_id, bundle_sha)

    profile = resolve_profile_dir()
    seed = pin_fingerprint_seed(profile)
    opts = build_launch_options(profile, seed)
    context = await launch(opts)
    page = context.pages[0] if context.pages else await context.new_page()
    transport = Transport(page)
    transport.arm_send_observer()

    await page.goto(CHATGPT_URL, wait_until="domcontentloaded")
    # Confirm the composer is present (logged in).
    try:
        await page.locator(SEL_COMPOSER).first.wait_for(state="visible", timeout=20000)
    except Exception:
        await context.close()
        return RunnerOutcome(
            ok=False,
            error_code=None,
            hint="composer not visible — not logged in?",
            delivery_state=DeliveryState.NOTHING_SENT,
            submitted=Submitted.FALSE,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    # Record the submitted user turn id from the conversation after submit; here
    # we type + submit ONE send.
    await transport.type_prompt(prompt)
    submit = await transport.submit_and_confirm(timeout_s=30.0)
    conv_id = submit.conversation_id
    if submit.delivery_state is not DeliveryState.DELIVERED or not conv_id:
        await context.close()
        return RunnerOutcome(
            ok=False,
            error_code=None,
            hint=f"delivery={submit.delivery_state.value}",
            delivery_state=submit.delivery_state,
            submitted=Submitted.UNKNOWN,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            bundle_sha256=bundle_sha,
        )

    # Resolve the submitted user-turn message id from the first GET.
    probe = await transport.read_conversation(conv_id)
    body = probe.get("body") or {}
    submitted_user_msg_id = _latest_user_msg_id(body)

    answer = await transport.poll_to_gate(
        conv_id, submitted_user_msg_id, run_id, bundle_sha, timeout_s=300.0
    )
    report = build_report(
        body_markdown=answer.text,
        artifact_path="(live-spike, no pinned artifact)",
        artifact_sha256="0" * 64,
        bundle_sha256=bundle_sha,
        model_slug=answer.model_slug,
        thinking_effort=answer.thinking_effort,
        run_id=run_id,
    )
    out_path = Path("/data/cao-scratch/worker-scratch/f862-build/live-spike-report.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
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
    )


def _latest_user_msg_id(conv: dict[str, object]) -> str:
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
        if author.get("role") == "user":
            ct = float(msg.get("create_time") or 0.0)
            if ct >= best_ct:
                best_ct = ct
                best = str(msg.get("id") or "")
    return best


def main() -> int:
    outcome = asyncio.run(_run())
    # Print the envelope only — never a secret. answer is truncated for the log.
    env = outcome.to_envelope()
    if env.get("answer"):
        env["answer"] = env["answer"][:400]
    import json

    print(json.dumps(env, indent=2, sort_keys=True))
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    sys.exit(main())
