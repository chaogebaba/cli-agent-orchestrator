"""AC-25/AC-35 post-invocation arms (F862 Amendment D, B1 closure).

The pre-invocation matrix lives in ``test_f862_real_browser_oracle.py``. This
module covers the other half the readiness review demanded: the cuts that land
*after* Python has already invoked the origin once -- mid-SSE, during the
authoritative GET, and immediately before ``route.fulfill``.

Every arm is real: a real Chromium page issues the routed POST, the production
``capture_held_route`` handler holds it, the production ``send_once`` makes one
real ``curl_cffi`` POST to the loopback origin, and the production
``poll_authoritative_get`` runs the GET. The adjudicator is again the fake
origin's server-side receipt ledger.

The load-bearing assertion is the RECEIPT IDENTITY, not the receipt count: the
origin must hold exactly one receipt and its body digest must be the digest of
the body **Python** posted, never the browser's captured body. A run where the
browser copy also reached the origin is D1's ``released_to_origin`` after an
invocation, which is the D11 build stop.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import time
from pathlib import Path
from test.fixtures.chatgpt_web_fake_origin import FakeOrigin
from test.fixtures.chatgpt_web_real_browser import (
    HeldRouteSession,
    RealBrowserHarness,
    browser_skip_condition,
    chromium_available,
    chromium_unavailable_reason,
    enforce_browser_requirement,
)
from typing import Any, Optional

import pytest

from cli_agent_orchestrator.chatgpt_web_runner.api_drive import send_once
from cli_agent_orchestrator.chatgpt_web_runner.detached_transport import (
    DetachedGetResult,
    poll_authoritative_get,
)
from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import (
    RouteCustodyError,
    RouteDisposition,
    synthetic_v1_stream,
)
from cli_agent_orchestrator.chatgpt_web_runner.poll_gate import (
    REQUIRED_MODEL_SLUG,
    REQUIRED_THINKING_EFFORT,
)
from cli_agent_orchestrator.chatgpt_web_runner.send_intent import (
    AttemptState,
    SendIntentLog,
)
from cli_agent_orchestrator.chatgpt_web_runner.stream_relay import AttemptRelay

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.slow,
    # The CI arm's selector: `-m browser_oracle` runs exactly these two modules
    # without having to re-enable every e2e+slow test in the suite.
    pytest.mark.browser_oracle,
    pytest.mark.asyncio,
    pytest.mark.xdist_group("f862-real-browser"),
    # Skips only an EXPLORATORY run. Under CAO_F862_REQUIRE_BROWSER the
    # condition goes False so the arms run and the autouse guard below
    # fails them loudly (B1 review fix 2).
    pytest.mark.skipif(
        browser_skip_condition(),
        reason=f"real-browser oracle needs Chromium: {chromium_unavailable_reason()}",
    ),
]


@pytest.fixture(autouse=True)
def _browser_verdict_guard() -> None:
    """A verdict run may not be satisfied by skips (B1 review fix 2)."""
    enforce_browser_requirement()


EVIDENCE_PATH = os.environ.get("CAO_F862_EVIDENCE")

#: Two frames so the origin has something to withhold at the mid-stream gate.
SLOW_SSE = b'event: delta_encoding\ndata: "v1"\n\ndata: [DONE]\n\n'

#: What PYTHON posts. Deliberately different bytes from the page's own body, so
#: the origin ledger can say WHICH copy it received rather than merely how many.
PYTHON_BODY = json.dumps(
    {
        "action": "next",
        "messages": [
            {
                "id": "user-python-1",
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": ["nonce-real python-posted"]},
            }
        ],
        "conversation_id": "fake-conversation",
        "parent_message_id": "node-0",
    },
    separators=(",", ":"),
).encode()
PYTHON_BODY_SHA = hashlib.sha256(PYTHON_BODY).hexdigest()

RUN_ID = "run-real"
BUNDLE_SHA = "0" * 64
#: The gate is the REAL one: it requires turn identity, end_turn/status
#: agreement, the pinned model/effort metadata and exactly one terminal
#: sentinel. Building the fixture to satisfy it (rather than stubbing the gate)
#: is what makes "GET remained authoritative" mean anything in these arms.
ANSWER_TEXT = f"synthetic answer\nEND_REVIEW:{RUN_ID}:{BUNDLE_SHA}"

CONVERSATION_BODY = {
    "conversation_id": "fake-conversation",
    "current_node": "assistant-real-1",
    "mapping": {
        "node-0": {
            "id": "node-0",
            "message": None,
            "parent": None,
            "children": ["user-python-1"],
        },
        "user-python-1": {
            "id": "user-python-1",
            "message": {
                "id": "user-python-1",
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": ["nonce-real python-posted"]},
            },
            "parent": "node-0",
            "children": ["assistant-real-1"],
        },
        "assistant-real-1": {
            "id": "assistant-real-1",
            "message": {
                "id": "assistant-real-1",
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": [ANSWER_TEXT]},
                "status": "finished_successfully",
                "end_turn": True,
                "metadata": {
                    "model_slug": REQUIRED_MODEL_SLUG,
                    "resolved_model_slug": REQUIRED_MODEL_SLUG,
                    "thinking_effort": REQUIRED_THINKING_EFFORT,
                },
            },
            "parent": "user-python-1",
            "children": [],
        },
    },
}


def _record_evidence(row: dict) -> None:
    if not EVIDENCE_PATH:
        return
    path = Path(EVIDENCE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _insecure_session_factory(**kwargs: Any) -> Any:
    """The production sender, pointed at a self-signed loopback certificate."""
    from curl_cffi.requests import AsyncSession

    return AsyncSession(verify=False, **kwargs)


def _ledger(tmp_path: Any, attempt_id: str) -> SendIntentLog:
    log = SendIntentLog(tmp_path / f"attempt-{attempt_id}")
    log.create_locked_attempt(
        run_id="run-real",
        attempt_id=attempt_id,
        prompt_sha="0" * 64,
        deadline_at=time.time() + 600,
        profile_epoch="epoch-real",
        mint_id="mint-real",
        mint_ordinal=1,
        relay_token_hash="f" * 64,
        relay_token_expires_at=time.time() + 600,
    )
    for state in (
        AttemptState.OWNED_BROWSER_READY,
        AttemptState.CONNECTOR_READY,
        AttemptState.INPUT_READY,
        AttemptState.INTERCEPT_ARMED,
    ):
        log.transition(state)
    log.record_send_intent(
        conversation_id="fake-conversation",
        current_node="node-0",
        attempt_nonce="nonce-real",
    )
    log.record_relay_skipped(skipped_at=time.time())
    log.transition(AttemptState.COMPOSER_MINT_TRIGGERED)
    return log


def _record_held(log: SendIntentLog, session: HeldRouteSession) -> None:
    assert session.captured is not None
    gens = session.captured.route_holder.generations
    log.record_request_held(
        body_sha256=session.captured.body_sha256,
        header_names=session.captured.header_names,
        page_generation=gens.page,
        context_generation=gens.context,
        cdp_session_generation=gens.cdp_session,
    )


def _origin_get(origin: FakeOrigin) -> Any:
    """A real HTTPS GET against the fake origin's gated conversation endpoint."""

    async def get_conversation(conversation_id: str) -> DetachedGetResult:
        def _fetch() -> tuple[int, Optional[dict]]:
            import ssl as _ssl
            import urllib.request

            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            url = f"{origin.origin}/backend-api/conversation/{conversation_id}"
            with urllib.request.urlopen(url, context=ctx, timeout=30) as response:
                return int(response.status), json.loads(response.read().decode())

        status, body = await asyncio.to_thread(_fetch)
        return DetachedGetResult(status_code=status, body=body)

    return get_conversation


async def _hold(harness: RealBrowserHarness, session: HeldRouteSession) -> None:
    await harness.open_page()
    await harness.trigger_conversation_post()
    await asyncio.wait_for(session.entered.wait(), 20)


async def _invoke_python_once(
    session: HeldRouteSession, log: SendIntentLog, relay: AttemptRelay
) -> "asyncio.Task[Any]":
    """Start the production one-shot sender against the captured origin URL."""
    assert session.captured is not None
    return asyncio.ensure_future(
        send_once(
            session.captured,
            body=PYTHON_BODY,
            live_generations=session.generations.current,
            intent_log=log,
            relay=relay,
            session_factory=_insecure_session_factory,
        )
    )


def _assert_one_python_receipt(origin: FakeOrigin) -> None:
    """The origin holds exactly one POST and it is the one PYTHON sent."""
    receipts = [row for row in origin.ledger.snapshot() if row.method == "POST"]
    assert len(receipts) == 1, receipts
    assert receipts[0].path == "/backend-api/f/conversation"
    # Identity, not count: a browser copy that also escaped would carry the
    # page's own body digest and would be D1's forbidden double-origin outcome.
    assert (
        receipts[0].body_sha256 == PYTHON_BODY_SHA
    ), "the origin received a body Python did not post -- the browser copy escaped"


def _relay(log: SendIntentLog) -> AttemptRelay:
    return AttemptRelay(
        attempt_id="attempt-post",
        token_hash="f" * 64,
        expires_at=time.time() + 600,
        intent_log=log,
    )


# =====================================================================
# Arm 1 — teardown MID-SSE, after the mint is spent
# =====================================================================


@pytest.mark.timeout(240)
async def test_arm_mid_sse_teardown_after_python_invocation(tmp_path):
    """Browser dies while the origin stream is still draining into the relay."""
    attempt_id = "attempt-post"
    with FakeOrigin(
        tmp_path / "tls",
        response_mode="slow_sse",
        sse_body=SLOW_SSE,
        conversation_body=CONVERSATION_BODY,
    ) as origin:
        async with RealBrowserHarness(origin=origin) as harness:
            session = await harness.arm_route(attempt_id=attempt_id)
            await _hold(harness, session)
            log = _ledger(tmp_path, attempt_id)
            _record_held(log, session)

            relay = _relay(log)
            send_task = await _invoke_python_once(session, log, relay)

            # The origin has committed its receipt and emitted one frame.
            assert await asyncio.to_thread(origin.first_chunk_sent.wait, 30)
            assert log.record.attempt_state == AttemptState.PYTHON_POST_INVOKED.value
            assert log.record.reserved_at is not None

            # THE CUT: tear the page down mid-stream.
            await harness.inject_page_close()
            origin.release_stream.set()

            result = await asyncio.wait_for(send_task, 60)
            await session.settle()

            # The mint was spent exactly once and the drain still completed,
            # because relay/browser loss must never abort origin reconciliation.
            assert result.status_code == 200
            assert result.bytes_drained > 0
            _assert_one_python_receipt(origin)

            # The held browser copy can no longer be fulfilled.
            disposition = await session.holder.on_teardown()
            assert disposition is RouteDisposition.LOST
            with pytest.raises(RouteCustodyError):
                await session.holder.fulfil(body=b"never")

            log.record_ack_unknown(route_disposition="lost", page_disposition="closed")
            assert log.record.attempt_state == AttemptState.ACK_UNKNOWN.value
            assert log.can_fresh_same_turn_mint() is False
            _record_evidence(
                {
                    "arm": "post_mid_sse",
                    "disposition": disposition.value,
                    "origin_receipts": len(origin.ledger.snapshot()),
                    "receipt_is_python_body": True,
                    "bytes_drained": result.bytes_drained,
                    "terminal": log.record.attempt_state,
                }
            )
            harness.release_holder()


# =====================================================================
# Arm 2 — teardown DURING the authoritative GET
# =====================================================================


@pytest.mark.timeout(240)
async def test_arm_during_get_teardown_after_python_invocation(tmp_path):
    """Browser dies while the detached GET is in flight; GET still decides."""
    attempt_id = "attempt-post"
    with FakeOrigin(
        tmp_path / "tls",
        response_mode="slow_sse",
        sse_body=SLOW_SSE,
        conversation_body=CONVERSATION_BODY,
    ) as origin:
        async with RealBrowserHarness(origin=origin) as harness:
            session = await harness.arm_route(attempt_id=attempt_id)
            await _hold(harness, session)
            log = _ledger(tmp_path, attempt_id)
            _record_held(log, session)

            relay = _relay(log)
            send_task = await _invoke_python_once(session, log, relay)
            assert await asyncio.to_thread(origin.first_chunk_sent.wait, 30)
            origin.release_stream.set()
            await asyncio.wait_for(send_task, 60)
            _assert_one_python_receipt(origin)

            log.transition(AttemptState.GET_VERIFY)
            poll = asyncio.ensure_future(
                poll_authoritative_get(
                    conversation_id="fake-conversation",
                    submitted_user_msg_id="user-python-1",
                    run_id=RUN_ID,
                    bundle_sha=BUNDLE_SHA,
                    deadline=time.monotonic() + 60,
                    get_conversation=_origin_get(origin),
                )
            )
            # THE CUT: the GET is genuinely in flight (the server is parked in
            # the handler) when the browser dies.
            assert await asyncio.to_thread(origin.get_gate_reached.wait, 30)
            await harness.inject_context_close()
            origin.release_get.set()

            answer, digest = await asyncio.wait_for(poll, 90)
            await session.settle()

            # GET remains authoritative even though the browser is gone.
            assert answer.assistant_node_id == "assistant-real-1"
            assert digest
            _assert_one_python_receipt(origin)

            disposition = await session.holder.on_teardown()
            assert disposition is RouteDisposition.LOST
            log.record_ack_unknown(route_disposition="lost", page_disposition="closed")
            assert log.can_fresh_same_turn_mint() is False
            _record_evidence(
                {
                    "arm": "post_during_get",
                    "disposition": disposition.value,
                    "origin_receipts": len(
                        [r for r in origin.ledger.snapshot() if r.method == "POST"]
                    ),
                    "receipt_is_python_body": True,
                    "get_accepted": answer.assistant_node_id,
                    "terminal": log.record.attempt_state,
                }
            )
            harness.release_holder()


# =====================================================================
# Arm 3 — teardown IMMEDIATELY BEFORE route.fulfill
# =====================================================================


@pytest.mark.timeout(240)
async def test_arm_immediately_before_fulfil_teardown(tmp_path):
    """The verified turn exists but the page died one instant before fulfil."""
    attempt_id = "attempt-post"
    with FakeOrigin(
        tmp_path / "tls",
        response_mode="slow_sse",
        sse_body=SLOW_SSE,
        conversation_body=CONVERSATION_BODY,
    ) as origin:
        async with RealBrowserHarness(origin=origin) as harness:
            session = await harness.arm_route(attempt_id=attempt_id)
            await _hold(harness, session)
            log = _ledger(tmp_path, attempt_id)
            _record_held(log, session)

            relay = _relay(log)
            send_task = await _invoke_python_once(session, log, relay)
            assert await asyncio.to_thread(origin.first_chunk_sent.wait, 30)
            origin.release_stream.set()
            await asyncio.wait_for(send_task, 60)
            log.transition(AttemptState.GET_VERIFY)
            origin.release_get.set()

            body = synthetic_v1_stream(
                posted_user_message=json.loads(PYTHON_BODY)["messages"][0],
                conversation_id="fake-conversation",
                assistant_id="assistant-real-1",
                final_text="synthetic answer",
            )

            # THE CUT: one instant before the local fulfil.
            await harness.inject_page_close()
            await session.settle()
            disposition = await session.holder.on_teardown()
            assert disposition is RouteDisposition.LOST

            with pytest.raises(RouteCustodyError):
                await session.holder.fulfil(body=body)

            # No second POST was provoked by the failed fulfil.
            _assert_one_python_receipt(origin)
            log.record_ack_unknown(route_disposition="lost", page_disposition="closed")
            assert log.can_fresh_same_turn_mint() is False
            _record_evidence(
                {
                    "arm": "post_before_fulfil",
                    "disposition": disposition.value,
                    "origin_receipts": len(
                        [r for r in origin.ledger.snapshot() if r.method == "POST"]
                    ),
                    "receipt_is_python_body": True,
                    "fulfil_refused": True,
                    "terminal": log.record.attempt_state,
                }
            )
            harness.release_holder()


# =====================================================================
# Arm 4 — the SUCCESS path, so the arms above are not vacuous
# =====================================================================


@pytest.mark.timeout(240)
async def test_success_path_one_origin_post_then_local_fulfil(tmp_path):
    """One Python POST, GET-verified, then the browser copy fulfilled locally."""
    attempt_id = "attempt-post"
    with FakeOrigin(
        tmp_path / "tls",
        response_mode="slow_sse",
        sse_body=SLOW_SSE,
        conversation_body=CONVERSATION_BODY,
    ) as origin:
        async with RealBrowserHarness(origin=origin) as harness:
            session = await harness.arm_route(attempt_id=attempt_id)
            await _hold(harness, session)
            log = _ledger(tmp_path, attempt_id)
            _record_held(log, session)

            relay = _relay(log)
            send_task = await _invoke_python_once(session, log, relay)
            assert await asyncio.to_thread(origin.first_chunk_sent.wait, 30)
            origin.release_stream.set()
            await asyncio.wait_for(send_task, 60)

            log.transition(AttemptState.GET_VERIFY)
            origin.release_get.set()
            answer, _digest = await asyncio.wait_for(
                poll_authoritative_get(
                    conversation_id="fake-conversation",
                    submitted_user_msg_id="user-python-1",
                    run_id=RUN_ID,
                    bundle_sha=BUNDLE_SHA,
                    deadline=time.monotonic() + 60,
                    get_conversation=_origin_get(origin),
                ),
                90,
            )
            assert answer.assistant_node_id == "assistant-real-1"

            body = synthetic_v1_stream(
                posted_user_message=json.loads(PYTHON_BODY)["messages"][0],
                conversation_id="fake-conversation",
                assistant_id="assistant-real-1",
                final_text="synthetic answer",
            )
            disposition = await session.holder.fulfil(body=body)
            await session.settle()

            assert disposition is RouteDisposition.FULFILLED
            # The whole point: the local fulfil produced NO extra origin POST.
            _assert_one_python_receipt(origin)

            for _ in range(60):
                text = await harness.result_text()
                if "synthetic answer" in text:
                    break
                await asyncio.sleep(0.1)
            assert "synthetic answer" in text

            log.transition(AttemptState.BROWSER_FULFIL)
            _record_evidence(
                {
                    "arm": "post_success_path",
                    "disposition": disposition.value,
                    "origin_receipts": len(
                        [r for r in origin.ledger.snapshot() if r.method == "POST"]
                    ),
                    "receipt_is_python_body": True,
                    "page_rendered_local_stream": True,
                    "terminal": log.record.attempt_state,
                }
            )
            harness.release_holder()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(session.finished.wait(), 10)
