"""Production run wiring — binds ``run_review`` to REAL seams (D2, r3).

This is the ONE production path a dispatched ``design_findings`` terminal runs.
``ChatGptWebProvider`` launches the runner (``python -m
cli_agent_orchestrator.chatgpt_web_runner``) in its tmux pane; the entrypoint
(:mod:`__main__`) reads the task and calls :func:`run_production_review`, which
wires every helper the r2 gate flagged as a dead caller onto this single path:

- ``ProfileLock`` (D5) — acquired by the PROVIDER around the whole turn (owner
  lease, owner-only stop); the runner is the lease owner's child.
- ``run_review`` (D2 anchor sequence) — start ``verify_pin`` → browser turn →
  ``build_report`` (D10 schema) → before-publication ``verify_pin`` → publish →
  worker-scoped callback.
- ``enforce_no_api_egress`` (D3) — installed as a request guard on the live page
  BEFORE any navigation, so a dynamically constructed OpenAI-API host is denied
  at request time (AC-11), not by a startup string scan.
- ``verify_attachment_on_turn`` (D8/AC-9) — run on the submitted turn, comparing
  the full attachment identity tuple INCLUDING the observed composer-side
  reference.

Identity: this module runs INSIDE the worker's own subprocess, so
``authority_pin_service.verify_pin`` (env principal = the worker) and the
inbox callback (``X-CAO-Terminal-Token`` = the worker's) act as the worker — the
anchor sequence's "assigned worker owns identity, report and callback" (D2).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
)
from cli_agent_orchestrator.chatgpt_web_runner.orchestrator import ReviewRequest, run_review
from cli_agent_orchestrator.chatgpt_web_runner.output import RunnerOutcome, Submitted
from cli_agent_orchestrator.chatgpt_web_runner.poll_gate import AcceptedAnswer

logger = logging.getLogger(__name__)


#: D7/D16: the "original attempt deadline" the single recovery must fall inside.
#: Generous relative to the 40s submit-confirm window and the 420s poll bound —
#: it exists to stop a recovery being spent hours later by a restarted process,
#: not to compete with the per-phase timeouts.
_ATTEMPT_DEADLINE_S = 1800.0


def _verify_pin_real(file_path: str) -> bool:
    """Real start/before-publication pin check as the WORKER (D2/AC-1/AC-2).

    Uses ``authority_pin_service.verify_pin`` whose principal is this process's
    ``CAO_TERMINAL_ID`` (the worker). The pin is INTACT when the local bytes match
    the CURRENT registered version — verdict ``VALID`` (v1) or ``SUPERSEDED`` (a
    warm-reused worker whose pins were rotated, F495: the current bytes still
    match). ``DRIFT`` / ``UNPINNED`` are non-intact → the orchestrator
    refuses/cancels publication. AC-2's "superseded cancels" is a CHANGE BETWEEN
    the two checks (start intact, before-publish drifted), which the two
    independent calls in run_review detect; a stable SUPERSEDED at both checks is
    pin-intact.
    """
    from cli_agent_orchestrator.services import authority_pin_service

    try:
        verdict = authority_pin_service.verify_pin(file_path)
    except Exception as exc:  # pragma: no cover - service/DB hiccup
        logger.warning("chatgpt_web verify_pin failed for %s: %s", file_path, type(exc).__name__)
        return False
    return bool(verdict.get("verdict") in ("VALID", "SUPERSEDED"))


def _callback_as_worker(message: str) -> None:
    """Deliver the worker-scoped callback to the recorded caller (D2).

    Posts to ``POST /terminals/{caller}/inbox/messages`` as the worker
    (``X-CAO-Terminal-Token`` from env), mirroring what the ``send_message`` MCP
    tool does with ``receiver_id`` omitted. The caller id comes from
    ``CAO_CALLBACK_TERMINAL_ID`` (recorded caller) — never guessed.
    """
    import requests

    endpoint = os.environ.get("CAO_ENDPOINT", "http://127.0.0.1:8990")
    token = os.environ.get("CAO_TERMINAL_TOKEN", "")
    sender = os.environ.get("CAO_TERMINAL_ID", "")
    receiver = os.environ.get("CAO_CALLBACK_TERMINAL_ID") or os.environ.get("CAO_CALLER_ID", "")
    if not receiver:
        raise RunnerError(
            RunnerErrorCode.SUBMIT_UNKNOWN,
            "no recorded caller (CAO_CALLBACK_TERMINAL_ID unset) — cannot call back",
            delivery_state=DeliveryState.DELIVERED,
        )
    headers = {"X-CAO-Terminal-Token": token} if token else {}
    resp = requests.post(
        f"{endpoint.rstrip('/')}/terminals/{receiver}/inbox/messages",
        params={"sender_id": sender, "message": message},
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()


def run_production_review(
    *,
    task_text: str,
    artifact_path: str,
    bundle_path: Optional[str],
    display_env: Optional[dict[str, str]] = None,
    callback: Any = _callback_as_worker,
    verify_pin: Any = _verify_pin_real,
    browser_turn: Any = None,
    on_stream_event: Any = None,
    relay: Any = None,
) -> RunnerOutcome:
    """Drive one production review turn through the D2 anchor sequence.

    ``callback`` and ``verify_pin`` are injectable for the assign integration
    test (stubbed browser/pin/callback seams); production binds the real ones
    above. Returns the :class:`RunnerOutcome`; the caller (``__main__``) prints
    the status markers and exit code from it.
    """
    import asyncio

    from cli_agent_orchestrator.chatgpt_web_runner.snapshot_upload import (
        build_attachment_identity,
        enforce_bundle_bounds,
        sha256_bytes,
        sha256_text,
    )

    started = time.monotonic()
    from cli_agent_orchestrator.chatgpt_web_runner.submit_ids import new_run_id

    run_id = new_run_id()

    if bundle_path:
        data = Path(bundle_path).read_bytes()
        enforce_bundle_bounds(data)
        bundle_sha = sha256_bytes(data)
        # The design_findings bundle is a TEXT bundle; upload it with a .txt name
        # (the file type the upload-probe calibrated the two readiness signals
        # against — a .md chip shows no "Document" label, F862 r2). Copy under a
        # .txt name in scratch when the source is not already .txt.
        upload_path = bundle_path
        if not bundle_path.endswith(".txt"):
            scratch = Path(
                os.environ.get("CAO_ARTIFACTS_DIR") or "/data/cao-scratch/worker-scratch/f862-build"
            )
            scratch.mkdir(parents=True, exist_ok=True)
            txt = scratch / (Path(bundle_path).stem + ".bundle.txt")
            txt.write_bytes(data)
            upload_path = str(txt)
        manifest_identity = build_attachment_identity(data, Path(upload_path).name)
    else:
        bundle_sha = sha256_text(task_text)
        manifest_identity = None
        upload_path = None

    request = ReviewRequest(
        artifact_path=artifact_path,
        artifact_sha256=bundle_sha,
        bundle_sha256=bundle_sha,
        run_id=run_id,
    )

    # Holder the browser turn fills with the OBSERVED attachment identity (incl.
    # the composer-side reference) so the envelope carries it (D8/AC-9).
    _observed_attachment: dict[str, Any] = {}
    # F970 (#819): holder for the teed send-stream facts (token/frame counts,
    # observed status/model, limits_progress quota). Diagnostic; D6 unchanged.
    _stream_facts: dict[str, Any] = {}

    # F862 (#718) D7/D16 r6 — open the DURABLE send-intent record for this
    # attempt BEFORE any browser work. Amendment C's owed-code row 1: at
    # 14256c1e delivery state was in-memory only, so a crash between intent and
    # dispatch could not be resolved. The record is fsynced to the attempt dir
    # and its counters survive a restart; the transport refuses to press Enter
    # without it, and refuses a SECOND press after one (D7/D16).
    from cli_agent_orchestrator.chatgpt_web_runner.send_intent import SendIntentLog

    _attempt_dir = (
        Path(os.environ.get("CAO_ARTIFACTS_DIR") or "/data/cao-scratch/worker-scratch/f862-build")
        / "attempts"
        / run_id
    )
    _intent_log = SendIntentLog(_attempt_dir)
    _intent_log.open_attempt(
        run_id=run_id,
        attempt_id=run_id,
        prompt_sha=sha256_text(task_text),
        deadline_at=time.time() + _ATTEMPT_DEADLINE_S,
    )

    # F970 (#819) step 2 — forward the stream. The relay publishes this turn's
    # allow-listed progress to cao-server (``/chatgpt/turns/<id>/events``) so it
    # can be followed live with seq ids and replay. Best-effort and bounded: it
    # never blocks, slows or fails the turn, and ``relay_from_env`` returns None
    # outside a CAO worker or when CAO_CHATGPT_TURN_RELAY=0.
    from cli_agent_orchestrator.chatgpt_web_runner.stream_relay import relay_from_env

    _relay = relay if relay is not None else relay_from_env(run_id)
    if _relay is not None:
        _relay.started({"artifact_path": artifact_path, "bundle_sha256": bundle_sha})
    _sink = on_stream_event
    if _sink is None and _relay is not None:
        _sink = _relay.publish_stream_event

    def _default_browser_turn() -> AcceptedAnswer:
        # Append the terminal sentinel instruction with THIS run's id + bundle
        # sha so the gate's strip_sentinel finds exactly one terminal
        # END_REVIEW:<run-id>:<bundle-sha> (D6). The runner owns these values; the
        # dispatched prompt need not know them.
        framed = (
            f"{task_text.rstrip()}\n\n"
            f"When finished, end your reply with EXACTLY one terminal line and "
            f"nothing after it:\nEND_REVIEW:{run_id}:{bundle_sha}\n"
        )
        return asyncio.run(
            _drive_browser(
                task_text=framed,
                bundle_path=upload_path,
                manifest_identity=manifest_identity,
                run_id=run_id,
                bundle_sha=bundle_sha,
                observed_holder=_observed_attachment,
                intent_log=_intent_log,
                stream_holder=_stream_facts,
                on_stream_event=_sink,
            )
        )

    _turn = browser_turn if browser_turn is not None else _default_browser_turn

    def _publish(body: str) -> str:
        out_dir = Path(
            os.environ.get("CAO_ARTIFACTS_DIR") or "/data/cao-scratch/worker-scratch/f862-build"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"chatgpt-web-findings-{run_id}.md"
        out_path.write_text(body, encoding="utf-8")
        return str(out_path)

    outcome = run_review(
        request,
        verify_pin_start=lambda: verify_pin(artifact_path),
        verify_pin_before_publish=lambda: verify_pin(artifact_path),
        browser_turn=_turn,
        publish=_publish,
        attachment_identity=(_observed_attachment or None),
        validate_schema=bool(bundle_path),
        manifest_text=(
            Path(upload_path).read_text(encoding="utf-8", errors="ignore") if upload_path else None
        ),
        stream_snapshot=(_stream_facts or None),
    )

    # F970: close the turn's relayed stream with a terminal event, so every
    # follower's SSE connection ends instead of idling to its deadline. This
    # happens BEFORE the worker callback, mirroring report-before-callback: a
    # watcher must never see the turn still "running" after the caller has been
    # told it finished.
    if _relay is not None:
        summary = {
            "ok": outcome.ok,
            "delivery_state": outcome.delivery_state.value,
            "error_code": outcome.error_code.value if outcome.error_code else None,
            "report_path": outcome.report_path,
            "conversation_url": outcome.conversation_url,
            "quota": (_stream_facts or {}).get("quota"),
        }
        (_relay.finished if outcome.ok else _relay.failed)(summary)
        _relay.close()

    # Worker-scoped callback AFTER publication (report-before-callback, D10/AC-1).
    if outcome.ok and outcome.report_path:
        callback(
            f"F862 FINDINGS-READY {outcome.report_path} "
            f"sha256={outcome.report_body_sha256} conv={outcome.conversation_url}"
        )
    else:
        callback(
            f"F862 FINDINGS-{'INVALID' if outcome.error_code and outcome.error_code.value == 'report_invalid' else 'FAILED'} "
            f"code={outcome.error_code.value if outcome.error_code else 'none'} "
            f"delivery={outcome.delivery_state.value}"
        )
    logger.info(
        "chatgpt_web production review finished in %sms", int((time.monotonic() - started) * 1000)
    )
    return outcome


async def _drive_browser(
    *,
    task_text: str,
    bundle_path: Optional[str],
    manifest_identity: Any,
    run_id: str,
    bundle_sha: str,
    observed_holder: Optional[dict[str, Any]] = None,
    intent_log: Optional[Any] = None,
    stream_holder: Optional[dict[str, Any]] = None,
    on_stream_event: Any = None,
) -> AcceptedAnswer:
    """The real browser turn: launch, egress-guard, (attach), submit, poll (D3/D6).

    Wires ``enforce_no_api_egress`` (as a page request guard) and
    ``verify_attachment_on_turn`` (post-submit identity, D8/AC-9) onto the path.
    """
    from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import (
        SEL_COMPOSER,
        Transport,
    )
    from cli_agent_orchestrator.chatgpt_web_runner.runtime import (
        CHATGPT_URL,
        build_launch_options,
        launch,
        pin_fingerprint_seed,
        resolve_profile_dir,
    )
    from cli_agent_orchestrator.chatgpt_web_runner.snapshot_upload import (
        enforce_no_api_egress,
        verify_attachment_on_turn,
    )

    profile = resolve_profile_dir()
    seed = pin_fingerprint_seed(profile)
    context = await launch(build_launch_options(profile, seed))
    page = context.pages[0] if context.pages else await context.new_page()

    # D3/AC-11: deny OpenAI-API egress at REQUEST time (dynamic host). The guard
    # runs on every request the page issues, aborting a forbidden host.
    async def _egress_guard(route: Any) -> None:
        try:
            enforce_no_api_egress(route.request.url)
        except RunnerError:
            await route.abort()
            return
        await route.continue_()

    try:
        await page.route("**/*", _egress_guard)
    except Exception:  # pragma: no cover - routing optional
        pass

    transport = Transport(page, intent_log=intent_log)
    transport.arm_send_observer()
    # F970 (#819): arm the SSE tee BEFORE the first navigation — an init script
    # only applies to subsequent loads. Progress/quota only; D6 unchanged.
    armed = await transport.arm_sse_tee(on_event=on_stream_event)
    logger.info("chatgpt_web sse tee armed=%s", armed)
    await page.goto(CHATGPT_URL, wait_until="domcontentloaded")
    await page.locator(SEL_COMPOSER).first.wait_for(state="visible", timeout=20000)

    if bundle_path:
        identity = await transport.attach_file(bundle_path)
        if manifest_identity is not None:
            # D8/AC-9: compare the FULL identity tuple including the observed
            # composer-side reference on the submitted turn.
            verify_attachment_on_turn(manifest_identity, identity, 1)
        if observed_holder is not None:
            observed_holder.update(
                {
                    "file_sha256": identity.file_sha256,
                    "byte_length": identity.byte_length,
                    "line_count": identity.line_count,
                    "submitted_filename": identity.submitted_filename,
                    "composer_attachment_ref": identity.composer_attachment_ref,
                }
            )

    await transport.type_prompt(task_text)
    submit = await transport.submit_and_confirm(timeout_s=40.0)
    conv_id = submit.conversation_id
    if submit.delivery_state is not DeliveryState.DELIVERED or not conv_id:
        if stream_holder is not None:
            snap = transport.stream_snapshot()
            if snap is not None:
                stream_holder.clear()
                stream_holder.update(snap)
        await context.close()
        raise RunnerError(
            RunnerErrorCode.SUBMIT_UNKNOWN,
            f"delivery={submit.delivery_state.value}",
            delivery_state=submit.delivery_state,
        )

    submitted_uid = ""
    for _ in range(10):
        probe = await transport.read_conversation(conv_id)
        body = probe.get("body")
        if probe.get("ok") and isinstance(body, dict):
            from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import (
                _newest_user_msg_id,
            )

            submitted_uid = _newest_user_msg_id(body)
            if submitted_uid:
                break
        await page.wait_for_timeout(1500)

    try:
        answer = await transport.poll_to_gate(
            conv_id, submitted_uid, run_id, bundle_sha, timeout_s=420.0
        )
    finally:
        # The stream facts are published on EVERY exit — a quota or truncation
        # failure is exactly when the caller wants the last thing the stream saw.
        if stream_holder is not None:
            snap = transport.stream_snapshot()
            if snap is not None:
                stream_holder.clear()
                stream_holder.update(snap)
                logger.info(
                    "chatgpt_web stream: %s token events, %s frames (%s dropped), quota=%s",
                    snap.get("token_events"),
                    snap.get("frames_seen"),
                    snap.get("frames_dropped"),
                    snap.get("quota"),
                )
        await context.close()
    assert isinstance(answer, AcceptedAnswer)
    return answer
