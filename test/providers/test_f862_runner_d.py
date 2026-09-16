"""Amendment D runner seams and named adversarial mutants (offline)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

from cli_agent_orchestrator.chatgpt_web_runner.api_drive import (
    ApiDriveError,
    rewrite_first_use_body,
    validate_impersonation,
)
from cli_agent_orchestrator.chatgpt_web_runner.detached_transport import (
    DetachedGetResult,
    reconcile_ack_unknown,
)
from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import (
    HeldRoute,
    RouteCustodyError,
    RouteDisposition,
    RouteGenerations,
    synthetic_v1_stream,
)
from cli_agent_orchestrator.chatgpt_web_runner.poll_gate import canonical_conversation_digest
from cli_agent_orchestrator.chatgpt_web_runner.stream_relay import (
    AttemptRelay,
    RelayAccessError,
    hash_relay_token,
)

pytestmark = pytest.mark.unit


@dataclass
class FakeRoute:
    aborted: int = 0
    fulfilled: int = 0

    async def abort(self):
        self.aborted += 1

    async def fulfill(self, **_kwargs):
        self.fulfilled += 1


@dataclass
class FakeRequest:
    method: str = "POST"
    url: str = "https://origin/backend-api/f/conversation"


def _holder():
    route = FakeRoute()
    holder = HeldRoute(
        route,
        FakeRequest(),
        attempt_id="a",
        generations=RouteGenerations(1, 1, 1),
    )
    return route, holder


def test_disposition_oracle_requestfailed_is_lost_not_aborted():
    async def run():
        route, holder = _holder()
        assert await holder.observe("requestfailed") is RouteDisposition.LOST
        assert route.aborted == 0
        assert await holder.abort() is RouteDisposition.LOST

    asyncio.run(run())


def test_disposition_oracle_fulfil_events_are_fulfilled_not_origin_release():
    async def run():
        route, holder = _holder()
        # The local fulfil path enters FULFILLING before Playwright emits its
        # response/finished events; the event must not become released_to_origin.
        task = asyncio.create_task(holder.fulfil(body=b"data: [DONE]\n\n"))
        await asyncio.sleep(0)
        assert await holder.observe("response") is RouteDisposition.FULFILLED
        await task
        assert route.fulfilled == 1

    asyncio.run(run())


def test_disposition_oracle_response_while_held_is_released_to_origin():
    async def run():
        _route, holder = _holder()
        assert await holder.observe("response") is RouteDisposition.RELEASED_TO_ORIGIN
        with pytest.raises(RouteCustodyError):
            await holder.guard_for_python(RouteGenerations(1, 1, 1))

    asyncio.run(run())


def test_route_first_terminal_wins_mutant():
    async def run():
        _route, holder = _holder()
        assert await holder.observe("requestfailed") is RouteDisposition.LOST
        assert await holder.on_teardown() is RouteDisposition.LOST

    asyncio.run(run())


def test_relay_wrong_token_second_subscriber_and_late_closed():
    async def run():
        relay = AttemptRelay(
            attempt_id="a", token_hash=hash_relay_token("secret"), expires_at=10**12
        )
        with pytest.raises(RelayAccessError, match="relay_token_invalid"):
            await relay.bind("wrong")
        await relay.bind("secret")
        with pytest.raises(RelayAccessError, match="relay_already_bound"):
            await relay.bind("secret")
        await relay.finish()

    asyncio.run(run())


def test_relay_overflow_closes_subscriber_but_drain_can_continue():
    async def run():
        relay = AttemptRelay(
            attempt_id="a", token_hash=hash_relay_token("secret"), expires_at=10**12, queue_bytes=3
        )
        await relay.bind("secret")
        assert await relay.publish(b"abcd") is False
        assert relay.status == "truncated"
        # Drain side continues to consume the origin; it is merely no longer
        # delivered to the disconnected subscriber.
        assert await relay.publish(b"e") is False

    asyncio.run(run())


def test_synthetic_fulfil_carries_exact_posted_input_message():
    posted = {"id": "u1", "author": {"role": "user"}, "content": {"parts": ["rewritten"]}}
    body = synthetic_v1_stream(
        posted_user_message=posted,
        conversation_id="c1",
        assistant_id="a1",
        final_text="ok",
    )
    assert b"event: input_message" in body
    assert b'"rewritten"' in body
    assert b"data: [DONE]" in body


def test_body_rewrite_and_impersonation_posture_guard():
    raw = json.dumps(
        {"messages": [{"id": "old", "author": {"role": "user"}, "content": {"parts": ["old"]}}]}
    ).encode()
    body, message = rewrite_first_use_body(raw, prompt_text="new", user_message_id="u2")
    assert json.loads(body)["messages"][0]["id"] == "u2"
    assert message["content"]["parts"] == ["new"]
    with pytest.raises(ApiDriveError):
        validate_impersonation("unknown")


def test_canonicaliser_keeps_tool_result_but_ignores_volatile_fields():
    base = {
        "conversation_id": "c",
        "current_node": "n2",
        "mapping": {
            "n2": {
                "parent": "n1",
                "message": {
                    "author": {"role": "tool"},
                    "content": {"parts": ["digest:abc"]},
                    "result_digest": "abc",
                    "create_time": 1,
                },
            }
        },
    }
    changed_volatile = json.loads(json.dumps(base))
    changed_volatile["mapping"]["n2"]["message"]["create_time"] = 999
    assert canonical_conversation_digest(base) == canonical_conversation_digest(changed_volatile)
    changed_result = json.loads(json.dumps(base))
    changed_result["mapping"]["n2"]["message"]["result_digest"] = "xyz"
    assert canonical_conversation_digest(base) != canonical_conversation_digest(changed_result)


def test_ack_unknown_finds_nonce_bearing_user_without_any_resend():
    body = {
        "conversation_id": "c",
        "current_node": "u",
        "mapping": {
            "u": {
                "message": {
                    "id": "user-landed",
                    "author": {"role": "user"},
                    "content": {"parts": ["work nonce-123"]},
                }
            }
        },
    }

    async def get(_conversation_id):
        return DetachedGetResult(200, body)

    result = asyncio.run(
        reconcile_ack_unknown(
            conversation_id="c",
            pre_send_current_node="old",
            attempt_nonce="nonce-123",
            user_message_id=None,
            deadline=10,
            get_conversation=get,
            clock=lambda: 0,
        )
    )
    assert result.delivery_seen is True
    assert result.user_message_id == "user-landed"


def test_ack_unknown_brand_new_missed_intercept_is_immediately_irreconcilable():
    calls = 0

    async def get(_conversation_id):
        nonlocal calls
        calls += 1
        return DetachedGetResult(200, {})

    result = asyncio.run(
        reconcile_ack_unknown(
            conversation_id=None,
            pre_send_current_node=None,
            attempt_nonce="n",
            user_message_id=None,
            deadline=10,
            get_conversation=get,
        )
    )
    assert result.irreconcilable is True
    assert calls == 0  # no forbidden conversation-list fallback


def test_ack_unknown_absence_is_observed_not_called_non_delivery():
    now = 0.0

    async def get(_conversation_id):
        return DetachedGetResult(
            200, {"conversation_id": "c", "current_node": "old", "mapping": {}}
        )

    async def sleep(seconds):
        nonlocal now
        now += seconds

    result = asyncio.run(
        reconcile_ack_unknown(
            conversation_id="c",
            pre_send_current_node="old",
            attempt_nonce="missing",
            user_message_id=None,
            deadline=10,
            get_conversation=get,
            sleep=sleep,
            clock=lambda: now,
        )
    )
    assert result.delivery_seen is False
    assert result.absence_observed is True
    assert result.irreconcilable is False


def test_ack_unknown_honours_retry_after_and_pending_backoff():
    now = 0.0
    sleeps = []
    results = iter(
        [
            DetachedGetResult(429, None, retry_after=7),
            DetachedGetResult(200, {"current_node": "old", "mapping": {}}),
        ]
    )

    async def get(_conversation_id):
        return next(results)

    async def sleep(seconds):
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    asyncio.run(
        reconcile_ack_unknown(
            conversation_id="c",
            pre_send_current_node="old",
            attempt_nonce="missing",
            user_message_id=None,
            deadline=17,
            get_conversation=get,
            sleep=sleep,
            clock=lambda: now,
        )
    )
    assert sleeps == [7, 10]
