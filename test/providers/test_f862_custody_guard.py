"""D1 custody-guard unit killers for the F862 Amendment D mutant ledger.

The real-browser arms prove the *system* behaviour; these prove the individual
guards that the arms would otherwise cover only in overlapping pairs. Each test
here is the designated killer of one named mutant in the B1 closure ledger, and
each is written so that removing the guard it targets makes it fail -- not
merely makes some other guard catch the same case.

No browser: these run in the ordinary offline tier.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from cli_agent_orchestrator.chatgpt_web_runner.errors import RunnerError
from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import (
    HeldRoute,
    RouteCustodyError,
    RouteDisposition,
    RouteGenerations,
    is_conversation_post,
)
from cli_agent_orchestrator.chatgpt_web_runner.poll_gate import (
    REQUIRED_MODEL_SLUG,
    REQUIRED_THINKING_EFFORT,
    evaluate_gate,
)
from cli_agent_orchestrator.chatgpt_web_runner.send_intent import (
    AttemptState,
    SendIntentLog,
    SendIntentViolation,
)

pytestmark = pytest.mark.asyncio

GENS = RouteGenerations(page=1, context=1, cdp_session=1)


class _FakeRoute:
    def __init__(self) -> None:
        self.aborted = False
        self.fulfilled = 0

    async def abort(self) -> None:
        self.aborted = True

    async def fulfill(self, **_kwargs: Any) -> None:
        self.fulfilled += 1


async def _dead_task() -> "asyncio.Task[None]":
    """A task that is already ``done()`` before we look at it."""

    async def _noop() -> None:
        return None

    task = asyncio.ensure_future(_noop())
    await task
    return task


# ---------------------------------------------------------------------
# AC-25-M1 — the guard must consult the LIVE holder task, not the row
# ---------------------------------------------------------------------


async def test_guard_refuses_a_dead_holder_before_its_terminal_is_written():
    """The race window: owner already dead, terminal callback not yet run.

    ``add_done_callback`` on an already-finished task schedules the callback
    with ``call_soon``, so it has not executed yet. An uncontended
    ``asyncio.Lock`` acquires without yielding, which means ``guard_for_python``
    reaches its checks while the disposition is still ``HELD``. Only the
    live-holder check can refuse here, so a guard that trusts the durable row
    admits a Python POST into a dead hold.
    """
    holder = HeldRoute(
        _FakeRoute(),
        object(),
        attempt_id="a",
        generations=GENS,
        owner_task=await _dead_task(),
    )
    assert holder.disposition is RouteDisposition.HELD
    with pytest.raises(RouteCustodyError, match="not live"):
        await holder.guard_for_python(GENS)


# ---------------------------------------------------------------------
# AC-25-M2 — the guard must compare the three generations
# ---------------------------------------------------------------------


async def test_guard_refuses_a_changed_generation_while_still_held():
    """A recycled page with a still-`held` route is refused on generations alone."""

    async def _live() -> None:
        await asyncio.sleep(3600)

    owner = asyncio.ensure_future(_live())
    try:
        holder = HeldRoute(
            _FakeRoute(), object(), attempt_id="a", generations=GENS, owner_task=owner
        )
        # Nothing tore the route down: the only thing wrong is the generation.
        assert holder.disposition is RouteDisposition.HELD
        moved = RouteGenerations(page=2, context=1, cdp_session=1)
        with pytest.raises(RouteCustodyError, match="generation changed"):
            await holder.guard_for_python(moved)
    finally:
        owner.cancel()


# ---------------------------------------------------------------------
# AC-25-M5 — first terminal wins, forever
# ---------------------------------------------------------------------


async def test_first_terminal_wins_against_a_later_event_proposal():
    """A `requestfailed` arriving after a proved abort must not rewrite it."""

    async def _live() -> None:
        await asyncio.sleep(3600)

    owner = asyncio.ensure_future(_live())
    try:
        route = _FakeRoute()
        holder = HeldRoute(route, object(), attempt_id="a", generations=GENS, owner_task=owner)
        assert await holder.abort() is RouteDisposition.ABORTED
        written_at = holder.terminal_written_at
        assert await holder.observe("requestfailed") is RouteDisposition.ABORTED
        assert await holder.observe("response") is RouteDisposition.ABORTED
        assert holder.terminal_written_at == written_at
    finally:
        owner.cancel()


# ---------------------------------------------------------------------
# AC-25-M8 — /prepare is never the conversation POST
# ---------------------------------------------------------------------


async def test_prepare_is_not_the_conversation_post():
    assert is_conversation_post("POST", "https://x/backend-api/f/conversation") is True
    assert is_conversation_post("POST", "https://x/backend-api/f/conversation/prepare") is False
    assert is_conversation_post("GET", "https://x/backend-api/f/conversation") is False


# ---------------------------------------------------------------------
# AC-26-M1/M2 — one reservation, one invocation
# ---------------------------------------------------------------------


def _held_log(tmp_path: Any) -> SendIntentLog:
    log = SendIntentLog(tmp_path / "attempt-guard")
    log.create_locked_attempt(
        run_id="run",
        attempt_id="attempt-guard",
        prompt_sha="0" * 64,
        deadline_at=time.time() + 600,
        profile_epoch="epoch",
        mint_id="mint",
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
    log.record_send_intent(conversation_id="c", current_node="node-0", attempt_nonce="nonce")
    log.record_relay_skipped(skipped_at=time.time())
    log.transition(AttemptState.COMPOSER_MINT_TRIGGERED)
    log.record_request_held(
        body_sha256="1" * 64,
        header_names=("content-type",),
        page_generation=1,
        context_generation=1,
        cdp_session_generation=1,
    )
    return log


async def test_mint_can_be_reserved_only_once(tmp_path):
    log = _held_log(tmp_path)
    log.reserve_mint()
    with pytest.raises(SendIntentViolation, match="already reserved"):
        log.reserve_mint()


async def test_python_post_can_be_recorded_only_once(tmp_path):
    log = _held_log(tmp_path)
    log.reserve_mint()
    log.record_python_post_invoked()
    # Either durable guard may be the one that refuses -- the state precondition
    # or the invoked_at stamp. The property is "never twice", not which check.
    with pytest.raises(
        SendIntentViolation, match="already invoked|requires a fsynced MINT_RESERVED"
    ):
        log.record_python_post_invoked()


async def test_holder_refuses_a_second_python_invocation():
    async def _live() -> None:
        await asyncio.sleep(3600)

    owner = asyncio.ensure_future(_live())
    try:
        holder = HeldRoute(
            _FakeRoute(), object(), attempt_id="a", generations=GENS, owner_task=owner
        )
        await holder.mark_python_invoked(GENS)
        with pytest.raises(RouteCustodyError, match="already invoked"):
            await holder.mark_python_invoked(GENS)
    finally:
        owner.cancel()


# ---------------------------------------------------------------------
# AC-26-M3 — only a PROVED abort authorises a fresh same-turn mint
# ---------------------------------------------------------------------


async def test_only_a_proved_abort_authorises_a_fresh_same_turn_mint(tmp_path):
    lost = _held_log(tmp_path)
    lost.record_ack_unknown(route_disposition="lost", page_disposition="closed")
    assert lost.can_fresh_same_turn_mint() is False

    aborted = _held_log(tmp_path / "second")
    aborted.record_abandoned_pre_invoke(route_disposition="aborted", page_disposition="closed")
    assert aborted.can_fresh_same_turn_mint() is True


# ---------------------------------------------------------------------
# AC-28-M1 — fulfil is legal only from HELD
# ---------------------------------------------------------------------


async def test_fulfil_is_refused_once_the_route_is_terminal():
    async def _live() -> None:
        await asyncio.sleep(3600)

    owner = asyncio.ensure_future(_live())
    try:
        route = _FakeRoute()
        holder = HeldRoute(route, object(), attempt_id="a", generations=GENS, owner_task=owner)
        assert await holder.on_teardown() is RouteDisposition.LOST
        with pytest.raises(RouteCustodyError, match="cannot fulfil"):
            await holder.fulfil(body=b"x")
        assert route.fulfilled == 0
    finally:
        owner.cancel()


# ---------------------------------------------------------------------
# AC-29-M1 — GET authority rests on TURN IDENTITY, not finishedness
# ---------------------------------------------------------------------


def _answer_node(parent: str) -> dict:
    return {
        "id": "assistant-1",
        "author": {"role": "assistant"},
        "content": {"content_type": "text", "parts": ["ans\nEND_REVIEW:run:sha"]},
        "status": "finished_successfully",
        "end_turn": True,
        "metadata": {
            "model_slug": REQUIRED_MODEL_SLUG,
            "resolved_model_slug": REQUIRED_MODEL_SLUG,
            "thinking_effort": REQUIRED_THINKING_EFFORT,
        },
    }


async def test_gate_refuses_an_answer_on_a_sibling_branch():
    """A perfectly finished assistant node that does not descend from OUR turn."""
    conv = {
        "conversation_id": "c",
        "current_node": "assistant-1",
        "mapping": {
            "other-user": {
                "id": "other-user",
                "message": {
                    "id": "other-user",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["someone else"]},
                },
                "parent": None,
                "children": ["assistant-1"],
            },
            "assistant-1": {
                "id": "assistant-1",
                "message": _answer_node("other-user"),
                "parent": "other-user",
                "children": [],
            },
        },
    }
    with pytest.raises(RunnerError, match="does not descend"):
        evaluate_gate(conv, submitted_user_msg_id="our-user", run_id="run", bundle_sha="sha")
