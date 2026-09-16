"""Production run wiring — the ONE composed Amendment D path (D2/D6, B3).

``ChatGptWebProvider`` launches the runner (``python -m
cli_agent_orchestrator.chatgpt_web_runner``) in its tmux pane; the entrypoint
(:mod:`__main__`) reads the task and calls :func:`run_production_review`.

What B3 changed, and why this docstring is worth reading before the code: at B1
this module still drove Amendment A's shape. It uploaded a bundle, let the
BROWSER send the conversation POST, read the conversation back through an
in-page ``fetch`` and polled the gate from inside the tab. Amendment D deletes
all of that (D10) and replaces it with ONE composed route:

    start_attempt (relay token minted atomically with the LOCKED row)
      -> connector listener bound on loopback for this attempt's manifest
      -> ONE route dispatcher installed BEFORE any composer action
      -> prompt typed, composer Enter pressed EXACTLY ONCE  (the mint)
      -> the conversation POST is HELD, never continued   (REQUEST_HELD)
      -> relay bound or explicitly skipped                (RELAY_BOUND_OR_SKIPPED)
      -> ONE Python POST via curl_cffi, drained to the raw relay
                                          (MINT_RESERVED -> PYTHON_POST_INVOKED)
      -> detached authoritative conversation GET          (GET_VERIFY)
      -> the held browser copy fulfilled LOCALLY from the posted object
                                                          (BROWSER_FULFIL)
      -> AC-33 source correlation, then publication       (VALIDATE/PUBLISH)

Two invariants are structural rather than checked after the fact:

* **The holder task is parked for the whole custody window.** ``HeldRoute``
  binds its owner to the route-handler task and writes the fail-closed ``lost``
  terminal when that task ends for ANY reason, including a normal return (see
  ``capture_held_route``'s contract). ``_HeldRouteCustody`` therefore parks the
  handler on an event and releases it only after fulfil/abort, which is also
  what keeps ``forbid_while_held`` meaningful.
* **There is no fallback.** No automatic retry between GUI send, Python send or
  any other transport exists inside one attempt (D10). A failed cut records its
  terminal (``ACK_UNKNOWN`` for a `lost` route, ``ABANDONED_PRE_INVOKE`` only
  for a proved holder-owned abort) and the attempt ends.

Identity is unchanged: this module runs INSIDE the worker's own subprocess, so
``authority_pin_service.verify_pin`` and the inbox callback act as the worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
)
from cli_agent_orchestrator.chatgpt_web_runner.orchestrator import ReviewRequest, run_review
from cli_agent_orchestrator.chatgpt_web_runner.output import RunnerOutcome
from cli_agent_orchestrator.chatgpt_web_runner.poll_gate import AcceptedAnswer

logger = logging.getLogger(__name__)


#: D7/D16: the "original attempt deadline" the single recovery must fall inside.
#: Generous relative to the 420s poll bound — it exists to stop a recovery being
#: spent hours later by a restarted process, not to compete with per-phase
#: timeouts.
_ATTEMPT_DEADLINE_S = 1800.0

#: How long the composer trigger has to produce the held conversation POST.
_HOLD_TIMEOUT_S = 60.0

#: The detached authoritative GET's bound (inherited cadence, D6).
_GET_TIMEOUT_S = 420.0


def _artifacts_dir() -> Path:
    return Path(
        os.environ.get("CAO_ARTIFACTS_DIR") or "/data/cao-scratch/worker-scratch/f862-build"
    )


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


@dataclass
class _HeldRouteCustody:
    """The parked route-handler task and everything observed about its route.

    This is the production counterpart of the oracle harness's
    ``HeldRouteSession``: it exists so the handler task — which is the route's
    OWNER — stays alive for the whole custody window instead of returning and
    classifying its own live route ``lost``.
    """

    attempt_id: str
    mint_id: str
    profile_epoch: str
    captured: Any = None
    holder: Any = None
    refused: Optional[str] = None
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    #: Public Playwright request events seen for the held request, in order.
    events: list[str] = field(default_factory=list)
    #: Observation tasks queued by those events; awaited before a disposition
    #: is read, so an assertion never races an event that has not landed.
    pending: "list[asyncio.Task[Any]]" = field(default_factory=list)

    async def settle(self, timeout: float = 2.0) -> None:
        await asyncio.sleep(0)
        if self.pending:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*list(self.pending), return_exceptions=True), timeout
                )
        await asyncio.sleep(0)


def run_production_review(
    *,
    task_text: str,
    artifact_path: str,
    source_manifest: Optional[list[str]] = None,
    frozen_worktree: Optional[str] = None,
    reviewed_commit: Optional[str] = None,
    base_commit: Optional[str] = None,
    display_env: Optional[dict[str, str]] = None,
    callback: Any = _callback_as_worker,
    verify_pin: Any = _verify_pin_real,
    browser_turn: Any = None,
) -> RunnerOutcome:
    """Drive one production review turn through the D2 anchor sequence.

    ``source_manifest`` is the attempt's allowlisted source list — the PULL
    plane's authority. There is no ``bundle_path``: Amendment D deleted pushed
    bundles (D10), so nothing is uploaded and no attachment identity is
    calibrated. ``callback``/``verify_pin``/``browser_turn`` stay injectable for
    the assign integration tests; production binds the real ones above.
    """
    from cli_agent_orchestrator.chatgpt_web_runner.snapshot_upload import sha256_text
    from cli_agent_orchestrator.chatgpt_web_runner.submit_ids import new_run_id

    started = time.monotonic()
    run_id = new_run_id()
    prompt_sha = sha256_text(task_text)
    manifest = list(source_manifest or [])

    request = ReviewRequest(
        artifact_path=artifact_path,
        artifact_sha256=prompt_sha,
        bundle_sha256=prompt_sha,
        run_id=run_id,
    )

    # D6: the relay token is minted ATOMICALLY with the pre-intent LOCKED row,
    # so there is never a window in which an attempt exists without its binding
    # secret's digest, and never a raw token on disk. This also creates the
    # durable attempt record BEFORE any browser work: a crash between here and
    # the mint leaves a resolvable attempt.
    from cli_agent_orchestrator.providers.chatgpt_web import ChatGptWebProvider

    handle = ChatGptWebProvider.start_attempt(
        run_id=run_id,
        attempt_id=run_id,
        prompt_sha=prompt_sha,
        deadline_s=_ATTEMPT_DEADLINE_S,
        profile_epoch=os.environ.get("CAO_TERMINAL_ID", "") or run_id,
        artifacts_dir=_artifacts_dir(),
    )

    from cli_agent_orchestrator.chatgpt_web_runner.send_intent import SendIntentLog

    intent_log = SendIntentLog(_artifacts_dir() / "attempts" / handle.attempt_id)
    # start_attempt wrote the LOCKED row from ITS OWN log object; this process's
    # log has to re-attach to those bytes or every later transition would raise
    # "no send-intent record in memory". load() is also the corrupt-record gate:
    # an unparseable row is UNRESOLVABLE and refuses rather than re-sending.
    if intent_log.load() is None:  # pragma: no cover - start_attempt just wrote it
        raise RunnerError(
            RunnerErrorCode.SUBMIT_UNKNOWN,
            f"attempt {handle.attempt_id} has no durable record after start_attempt",
            delivery_state=DeliveryState.NOTHING_SENT,
        )

    # ONE writer per attempt file. ``start_attempt`` registered the relay with
    # the log object IT created, which still holds the LOCKED record in memory.
    # Left alone, the relay's own ledger writes (bind / skip) would serialize
    # that stale record over the file and silently drop every transition this
    # process had made — and the next transition from this process would then
    # drop the relay row straight back. Re-pointing the relay at the loaded log
    # makes the two paths share one record instead of racing over one file.
    from cli_agent_orchestrator.chatgpt_web_runner.stream_relay import get_relay_hub

    get_relay_hub().get(handle.attempt_id).intent_log = intent_log

    #: Filled by the composed turn so publication can be correlated (AC-33).
    pull_evidence: dict[str, Any] = {}

    def _default_browser_turn() -> AcceptedAnswer:
        # Append the terminal sentinel instruction with THIS run's id + prompt
        # sha so the gate's strip_sentinel finds exactly one terminal
        # END_REVIEW:<run-id>:<sha> (D6). The runner owns these values; the
        # dispatched prompt need not know them.
        framed = (
            f"{task_text.rstrip()}\n\n"
            f"When finished, end your reply with EXACTLY one terminal line and "
            f"nothing after it:\nEND_REVIEW:{run_id}:{prompt_sha}\n"
        )
        return asyncio.run(
            _drive_composed_turn(
                task_text=framed,
                run_id=run_id,
                attempt_id=handle.attempt_id,
                prompt_sha=prompt_sha,
                intent_log=intent_log,
                manifest=manifest,
                frozen_worktree=frozen_worktree,
                reviewed_commit=reviewed_commit,
                base_commit=base_commit,
                pull_evidence=pull_evidence,
            )
        )

    _turn = browser_turn if browser_turn is not None else _default_browser_turn

    def _publish(body: str) -> str:
        out_dir = _artifacts_dir()
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
        attachment_identity=None,
        # A findings run always validates the report schema. At B1 this rode
        # ``bool(bundle_path)``; with pushed bundles deleted the lane is always
        # the findings lane, so the validation is unconditional rather than a
        # side effect of an upload.
        validate_schema=True,
        manifest_text=("\n".join(manifest) if manifest else None),
    )

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


async def _drive_composed_turn(
    *,
    task_text: str,
    run_id: str,
    attempt_id: str,
    prompt_sha: str,
    intent_log: Any,
    manifest: list[str],
    frozen_worktree: Optional[str],
    reviewed_commit: Optional[str],
    base_commit: Optional[str],
    pull_evidence: dict[str, Any],
) -> AcceptedAnswer:
    """The composed D path: mint -> hold -> one POST -> relay/GET -> fulfil.

    Every step below is ordered by the D6 state machine, and every durable
    transition is written BEFORE the action it authorises.
    """
    from cli_agent_orchestrator.chatgpt_web_runner.api_drive import (
        rewrite_first_use_body,
        send_once,
    )
    from cli_agent_orchestrator.chatgpt_web_runner.detached_transport import (
        poll_authoritative_get,
    )
    from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import (
        SEL_COMPOSER,
        LiveGenerations,
        RouteCustodyError,
        RouteDisposition,
        Transport,
        capture_held_route,
        is_conversation_post,
        synthetic_v1_stream,
    )
    from cli_agent_orchestrator.chatgpt_web_runner.runtime import (
        CHATGPT_URL,
        build_launch_options,
        launch,
        pin_fingerprint_seed,
        resolve_profile_dir,
    )
    from cli_agent_orchestrator.chatgpt_web_runner.send_intent import AttemptState
    from cli_agent_orchestrator.chatgpt_web_runner.snapshot_upload import enforce_no_api_egress
    from cli_agent_orchestrator.chatgpt_web_runner.source_pull import (
        ConnectorListener,
        collect_pull_evidence,
        verify_source_correlation,
    )
    from cli_agent_orchestrator.chatgpt_web_runner.stream_relay import get_relay_hub
    from cli_agent_orchestrator.chatgpt_web_runner.submit_ids import new_run_id

    profile = resolve_profile_dir()
    profile_epoch = pin_fingerprint_seed(profile)
    context = await launch(build_launch_options(profile, profile_epoch))
    page = context.pages[0] if context.pages else await context.new_page()
    generations = LiveGenerations(page, context)
    intent_log.transition(AttemptState.OWNED_BROWSER_READY)

    # ── the pull plane ────────────────────────────────────────────────────
    # One attempt, one loopback listener, separately killable. The connector —
    # not the prompt — is the authority on what may be read (D5).
    connector_listener: Optional[ConnectorListener] = None
    connector_server: Any = None
    if manifest and frozen_worktree:
        from cli_agent_orchestrator.services.workspace_read import bind_attempt

        connector_server = bind_attempt(
            attempt_id=attempt_id,
            frozen_worktree=frozen_worktree,
            manifest=manifest,
            reviewed_commit=reviewed_commit,
            base_commit=base_commit,
            state_dir=_artifacts_dir() / "attempts" / attempt_id / "connector",
        )
        connector_listener = ConnectorListener(connector_server)
        await connector_listener.start()
    intent_log.transition(AttemptState.CONNECTOR_READY)

    custody = _HeldRouteCustody(
        attempt_id=attempt_id, mint_id=attempt_id, profile_epoch=profile_epoch
    )

    async def _dispatcher(route: Any) -> None:
        """The ONE route dispatcher, installed before any composer action.

        Non-conversation traffic keeps its normal page behaviour unless the D3
        egress guard denies its host. The conversation POST is HELD and NEVER
        continued: there is no ``continue_()`` success branch for it, which is
        the deletion D10 asks for.
        """
        request = route.request
        try:
            method = str(getattr(request, "method", "GET"))
            url = str(getattr(request, "url", ""))
        except Exception:  # pragma: no cover - defensive
            await route.continue_()
            return
        if not is_conversation_post(method, url):
            try:
                enforce_no_api_egress(url)
            except RunnerError:
                await route.abort()
                return
            await route.continue_()
            return
        # A SECOND conversation POST is not this attempt's mint. One mint per
        # turn is the whole invariant, and the composer side is already guarded
        # by the durable record — but a page-initiated retry would otherwise
        # park a second handler forever, or quietly replace the captured route.
        # Aborting it is safe and conservative: that copy provably never reached
        # the origin, and the attempt's own held route is untouched.
        if custody.entered.is_set():
            custody.refused = "a second conversation POST was refused; one mint per turn"
            logger.warning("chatgpt_web refused a second conversation POST for %s", attempt_id)
            await route.abort()
            return
        # The conversation POST: capture, hold, and PARK so the owner task
        # outlives the custody window (capture_held_route's contract).
        try:
            captured = await capture_held_route(
                route,
                attempt_id=attempt_id,
                mint_id=attempt_id,
                profile_epoch=profile_epoch,
                generations=generations.current,
            )
        except RouteCustodyError as exc:
            custody.refused = str(exc)
            custody.finished.set()
            return
        custody.captured = captured
        custody.holder = captured.route_holder
        _wire_request_events(page, custody, captured.route_holder.request)
        custody.entered.set()
        try:
            await custody.release.wait()
        finally:
            custody.finished.set()

    await page.route("**/*", _dispatcher)
    await page.goto(CHATGPT_URL, wait_until="domcontentloaded")
    await page.locator(SEL_COMPOSER).first.wait_for(state="visible", timeout=20000)
    intent_log.transition(AttemptState.INPUT_READY)

    transport = Transport(page, intent_log=intent_log)
    await transport.type_prompt(task_text)
    # The dispatcher was installed before navigation, i.e. EARLIER than D6's
    # INTERCEPT_ARMED position requires. The row is written here, at the last
    # moment before the mint, so it records "still armed with the prompt in the
    # composer" rather than merely "was armed at some earlier point".
    intent_log.transition(AttemptState.INTERCEPT_ARMED)

    attempt_nonce = new_run_id()
    intent_log.record_send_intent(
        conversation_id=None,
        current_node=None,
        attempt_nonce=attempt_nonce,
    )

    # ── the relay binding window closes HERE, before the mint (D3/D6) ─────
    # The runner does NOT bind: the relay has exactly one subscriber and it is
    # whoever presented the token on the public CAO route. ``bind()`` mints the
    # subscriber id and writes RELAY_BOUND_OR_SKIPPED itself, so a runner that
    # called it would consume the single binding and lock the real subscriber
    # out. All the runner owes is the EXPLICIT close: if nobody has bound by the
    # time the mint is about to be reserved, the attempt records a skip, so the
    # durable row always says which of the two happened.
    relay = get_relay_hub().get(attempt_id)
    if not relay.is_bound:
        await relay.mark_skipped()

    answer: Optional[AcceptedAnswer] = None
    try:
        # ── the mint: EXACTLY ONE composer Enter ──────────────────────────
        intent_log.transition(AttemptState.COMPOSER_MINT_TRIGGERED)
        await transport.trigger_composer_mint()
        try:
            await asyncio.wait_for(custody.entered.wait(), _HOLD_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise RunnerError(
                RunnerErrorCode.SUBMIT_UNKNOWN,
                "the composer trigger produced no held conversation POST "
                f"within {_HOLD_TIMEOUT_S:.0f}s (intercept unproven)",
                delivery_state=DeliveryState.NOTHING_SENT,
            )
        captured = custody.captured
        holder = custody.holder
        assert captured is not None and holder is not None

        gens = captured.route_holder.generations
        intent_log.record_request_held(
            body_sha256=captured.body_sha256,
            header_names=captured.header_names,
            page_generation=gens.page,
            context_generation=gens.context,
            cdp_session_generation=gens.cdp_session,
        )
        transport._observe_send()

        # ── the ONE Python POST ───────────────────────────────────────────
        user_message_id = new_run_id()
        body, posted_user_message = rewrite_first_use_body(
            captured.raw_body,
            prompt_text=task_text,
            user_message_id=user_message_id,
        )
        origin = await send_once(
            captured,
            body=body,
            live_generations=generations.current,
            intent_log=intent_log,
            relay=relay,
        )

        conversation_id = origin.conversation_id
        if not conversation_id:
            raise RunnerError(
                RunnerErrorCode.SUBMIT_UNKNOWN,
                "the origin stream carried no conversation id; the attempt is ack-unknown",
                delivery_state=DeliveryState.ACK_UNKNOWN,
            )

        # ── the DETACHED authoritative GET ────────────────────────────────
        # Not the browser's: D10 removed the in-page conversation fetch as
        # publication transport.
        intent_log.transition(AttemptState.GET_VERIFY)
        answer, branch_digest = await poll_authoritative_get(
            conversation_id=conversation_id,
            submitted_user_msg_id=user_message_id,
            run_id=run_id,
            bundle_sha=prompt_sha,
            deadline=time.monotonic() + _GET_TIMEOUT_S,
            get_conversation=_detached_get(captured),
        )

        # ── the LOCAL synthetic fulfil ────────────────────────────────────
        # Built from the exact object Python posted, so the page renders the
        # answer it would have rendered — without a second origin request.
        disposition = await holder.fulfil(
            body=synthetic_v1_stream(
                posted_user_message=posted_user_message,
                conversation_id=conversation_id,
                assistant_id=answer.assistant_node_id,
                final_text=answer.text,
            )
        )
        await custody.settle()
        # D11 BUILD STOP. `released_to_origin` means the BROWSER's copy of the
        # conversation POST also reached the origin after Python had already
        # invoked it: two sends for one mint, which is the outcome Amendment D
        # exists to prevent. It is never recoverable and never publishable.
        if RouteDisposition.RELEASED_TO_ORIGIN in (disposition, holder.disposition):
            intent_log.record_ack_unknown(
                route_disposition=RouteDisposition.RELEASED_TO_ORIGIN.value,
                page_disposition="open",
            )
            raise RunnerError(
                RunnerErrorCode.SUBMIT_UNKNOWN,
                "D11 build stop: the held browser copy was released to the origin after "
                "the Python invocation — two sends for one mint",
                delivery_state=DeliveryState.ACK_UNKNOWN,
            )
        if disposition is RouteDisposition.FULFILLED:
            intent_log.transition(AttemptState.BROWSER_FULFIL)
        else:
            intent_log.record_ack_unknown(
                route_disposition=disposition.value, page_disposition="open"
            )

        # ── AC-33 source correlation, BEFORE publication ──────────────────
        if connector_server is not None:
            from cli_agent_orchestrator.api.routes_chatgpt_web_connector import (
                connector_audit_projection,
            )

            evidence = collect_pull_evidence(connector_audit_projection(connector_server))
            correlated = verify_source_correlation(
                evidence,
                answer_text=answer.text,
                observed_branch_digest=branch_digest,
                accepted_branch_digest=branch_digest,
            )
            pull_evidence["sources"] = [
                {"path": source.path, "digest": source.digest} for source in correlated
            ]
            pull_evidence["branch_digest"] = branch_digest

        # ── AC-20 counters gate acceptance ────────────────────────────────
        failure = intent_log.acceptance_failure()
        if failure:
            raise RunnerError(
                RunnerErrorCode.SUBMIT_UNKNOWN, failure, delivery_state=DeliveryState.ACK_UNKNOWN
            )
        return answer
    finally:
        # Release the parked holder FIRST so the owner task can end without
        # racing the teardown, then take the pull plane and the browser down.
        custody.release.set()
        # Only a handler that actually entered can finish; waiting on a route
        # that was never captured would burn the whole timeout for nothing.
        if custody.entered.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(custody.finished.wait(), 10)
        if connector_listener is not None:
            await connector_listener.stop()
        with contextlib.suppress(Exception):
            await context.close()


def _wire_request_events(page: Any, custody: _HeldRouteCustody, request: Any) -> None:
    """Feed the page's PUBLIC request events for the held request into the holder.

    Without this the D1 disposition oracle is blind in production. ``HeldRoute``
    decides `released_to_origin` vs `fulfilled` vs `lost` from
    ``requestfailed`` / ``requestfinished`` / ``response`` on the held request —
    and ``released_to_origin`` after a Python invocation is D11's build stop. A
    composition that never called ``observe()`` could not detect the one outcome
    the whole amendment exists to prevent, so the arms would be proving a
    property production does not have.

    Only PUBLIC Playwright events are used; no private field is inspected.
    """

    def _make(event_name: str) -> Any:
        def _on(payload: Any) -> None:
            observed = getattr(payload, "request", payload)
            if observed is not request and getattr(observed, "url", None) != getattr(
                request, "url", None
            ):
                return
            custody.events.append(event_name)
            custody.pending.append(asyncio.ensure_future(_observe(custody, event_name)))

        return _on

    for name in ("requestfailed", "requestfinished", "response"):
        with contextlib.suppress(Exception):
            page.on(name, _make(name))


async def _observe(custody: _HeldRouteCustody, event_name: str) -> None:
    holder = custody.holder
    if holder is None:  # pragma: no cover - defensive
        return
    await holder.observe(event_name)


def _detached_get(captured: Any) -> Any:
    """A conversation GET that does NOT run inside the browser (D10).

    It reuses the captured request's own credentials — the same ones the held
    POST carried — through ``curl_cffi``, so acceptance never depends on the tab
    still being alive, and a torn-down page cannot silently stop verification.
    """
    from typing import cast as _cast

    from cli_agent_orchestrator.chatgpt_web_runner.api_drive import (
        API_DRIVE_IMPERSONATE,
        _headers_and_cookies,
        validate_impersonation,
    )
    from cli_agent_orchestrator.chatgpt_web_runner.detached_transport import DetachedGetResult

    headers, cookies = _headers_and_cookies(captured)
    origin = captured.url.split("/backend-api/", 1)[0]

    async def get_conversation(conversation_id: str) -> DetachedGetResult:
        import json as _json

        from curl_cffi.requests import AsyncSession

        # Same allow-list gate the sender uses: curl-cffi 0.13 accepts unknown
        # templates silently, so an unvalidated posture must fail before network.
        validate_impersonation(API_DRIVE_IMPERSONATE)
        session = _cast(Any, AsyncSession)(impersonate=API_DRIVE_IMPERSONATE)
        for name, value, domain, path in cookies:
            session.cookies.set(name, value, domain=domain, path=path)
        try:
            response = await session.get(
                f"{origin}/backend-api/conversation/{conversation_id}",
                headers=[
                    (name, value) for name, value in headers if name.lower() != "content-type"
                ],
            )
            retry_after = response.headers.get("retry-after")
            body: Optional[dict[str, Any]] = None
            if int(response.status_code) == 200:
                with contextlib.suppress(ValueError):
                    body = dict(_json.loads(response.content))
            return DetachedGetResult(
                status_code=int(response.status_code),
                body=body,
                retry_after=float(retry_after) if retry_after else None,
            )
        finally:
            close = getattr(session, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result

    return get_conversation
