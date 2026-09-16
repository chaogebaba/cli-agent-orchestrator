"""AC-25 real-browser disposition oracle (F862 Amendment D, B1 closure).

Every test here drives a **real** Chromium page that issues the routed
conversation POST, through the **production** route handler
(``capture_held_route``), a live holder task and the production
``HeldRoute``/``SendIntentLog`` state machines. The adjudicator is the fake
origin's own receipt ledger, which is written server-side before any response
action and is therefore independent of Playwright's ``requestfailed`` /
``requestfinished`` / ``response`` claims.

The load-bearing property the readiness review demanded is the table proved by
``test_ledger_distinguishes_*``: two arms that produce the *same* Playwright
events differ in the ledger, and the holder's disposition follows the ledger,
not the events.

No test touches chatgpt.com, the logged-in profile, or any real endpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
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
from typing import Any

import pytest

from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import (
    RouteCustodyError,
    RouteDisposition,
    RouteGenerations,
    synthetic_v1_stream,
)
from cli_agent_orchestrator.chatgpt_web_runner.send_intent import (
    AttemptState,
    SendIntentLog,
    SendIntentViolation,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.slow,
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


#: When set, every arm appends its evidence row here as JSON lines, so the
#: readiness report cites what the run actually observed instead of a
#: transcription of it.
EVIDENCE_PATH = os.environ.get("CAO_F862_EVIDENCE")


def _record_evidence(row: dict) -> None:
    if not EVIDENCE_PATH:
        return
    path = Path(EVIDENCE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


POSTED_USER_MESSAGE = {
    "id": "user-real-1",
    "author": {"role": "user"},
    "content": {"content_type": "text", "parts": ["python-posted"]},
}


def _ledger(tmp_path: Any, attempt_id: str = "attempt-real") -> SendIntentLog:
    """A real durable ledger walked to REQUEST_HELD's predecessor states."""
    log = SendIntentLog(tmp_path / f"attempt-{attempt_id}")
    log.create_locked_attempt(
        run_id="run-real",
        attempt_id=attempt_id,
        prompt_sha="0" * 64,
        deadline_at=1e12,
        profile_epoch="epoch-real",
        mint_id="mint-real",
        mint_ordinal=1,
        relay_token_hash="f" * 64,
        relay_token_expires_at=1e12,
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
    log.record_relay_skipped(skipped_at=1.0)
    log.transition(AttemptState.COMPOSER_MINT_TRIGGERED)
    return log


async def _hold_and_capture(
    harness: RealBrowserHarness, session: HeldRouteSession, timeout: float = 20.0
) -> None:
    """Load the page, fire its fetch, and wait for the production hold."""
    await harness.open_page()
    await harness.trigger_conversation_post()
    await asyncio.wait_for(session.entered.wait(), timeout)


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


# =====================================================================
# 1. The hold itself is real
# =====================================================================


@pytest.mark.timeout(180)
async def test_real_page_post_is_held_by_the_production_handler(tmp_path):
    """The deterministic page's own fetch reaches capture_held_route and stops."""
    with FakeOrigin(tmp_path / "tls") as origin:
        async with RealBrowserHarness(origin=origin) as harness:
            session = await harness.arm_route()
            await _hold_and_capture(harness, session)

            assert session.captured is not None
            assert session.captured.method == "POST"
            assert session.captured.url.endswith("/backend-api/f/conversation")
            assert b'"fake-user"' in session.captured.raw_body
            assert session.disposition is RouteDisposition.HELD
            # The request never left the browser: the origin saw nothing. A
            # release started by the handler would land at the origin a few
            # milliseconds later, so give it a bounded chance to appear rather
            # than reading the ledger the instant the hold is announced -- an
            # immediate read passes for a handler that DID release the route.
            for _ in range(20):
                await asyncio.sleep(0.05)
                assert origin.ledger.snapshot() == (), origin.ledger.snapshot()

            log = _ledger(tmp_path)
            _record_held(log, session)
            assert log.record.attempt_state == AttemptState.REQUEST_HELD.value
            assert log.record.body_sha256 == session.captured.body_sha256

            harness.release_holder()
            await asyncio.wait_for(session.finished.wait(), 10)


# =====================================================================
# 2. The distinguishability table (the readiness gate's core demand)
# =====================================================================


@pytest.mark.timeout(180)
async def test_ledger_distinguishes_local_fulfil_from_origin_receipt(tmp_path):
    """Local fulfil: same Playwright events as a real origin turn, empty ledger."""
    body = synthetic_v1_stream(
        posted_user_message=POSTED_USER_MESSAGE,
        conversation_id="fake-conversation",
        assistant_id="assistant-real-1",
        final_text="synthetic answer",
    )

    async def fulfil_action(session: HeldRouteSession) -> None:
        assert session.holder is not None
        await session.holder.fulfil(body=body)

    with FakeOrigin(tmp_path / "tls") as origin:
        async with RealBrowserHarness(origin=origin) as harness:
            session = await harness.arm_route(action=fulfil_action)
            await _hold_and_capture(harness, session)
            await asyncio.wait_for(session.finished.wait(), 20)
            await session.settle()

            assert session.disposition is RouteDisposition.FULFILLED
            # The ORIGIN LEDGER is the discriminator: zero receipts.
            assert origin.ledger.snapshot() == ()
            # The browser really consumed the locally-authored stream.
            for _ in range(40):
                text = await harness.result_text()
                if "synthetic answer" in text:
                    break
                await asyncio.sleep(0.1)
            assert "synthetic answer" in text
            assert "[DONE]" in text
            # Playwright reported a completed request, exactly as a real turn would.
            assert "response" in session.events or "requestfinished" in session.events


@pytest.mark.timeout(180)
async def test_ledger_distinguishes_origin_receipt_then_reset_as_lost(tmp_path):
    """Origin received the POST, then reset: requestfailed must stay `lost`."""

    async def release_to_origin(session: HeldRouteSession) -> None:
        # MUTANT posture: a handler that releases the browser copy. Production
        # never does this for the conversation POST; it is how the harness
        # manufactures a genuine origin receipt to adjudicate against.
        assert session.captured is not None
        await session.captured.route_holder.route.continue_()
        await asyncio.wait_for(session.terminal.wait(), 20)

    with FakeOrigin(tmp_path / "tls", response_mode="reset") as origin:
        async with RealBrowserHarness(origin=origin) as harness:
            session = await harness.arm_route(action=release_to_origin)
            await _hold_and_capture(harness, session)
            await asyncio.wait_for(session.finished.wait(), 25)
            await session.settle()

            receipts = origin.ledger.snapshot()
            assert len(receipts) == 1, receipts
            assert receipts[0].path == "/backend-api/f/conversation"
            assert receipts[0].response_mode == "reset"
            assert "requestfailed" in session.events
            # AC-25 (ii): a requestfailed is NOT proof of non-delivery.
            assert session.disposition is RouteDisposition.LOST

            log = _ledger(tmp_path)
            _record_held(log, session)
            log.record_ack_unknown(route_disposition="lost", page_disposition="quarantined")
            assert log.record.attempt_state == AttemptState.ACK_UNKNOWN.value
            assert log.can_fresh_same_turn_mint() is False
            with pytest.raises(SendIntentViolation):
                log.record_abandoned_pre_invoke(
                    route_disposition="lost", page_disposition="quarantined"
                )


@pytest.mark.timeout(180)
async def test_response_while_held_is_released_to_origin_and_refuses_python(tmp_path):
    """A completed origin response while HELD is the forbidden double-send."""

    async def release_to_origin(session: HeldRouteSession) -> None:
        assert session.captured is not None
        await session.captured.route_holder.route.continue_()
        await asyncio.wait_for(session.terminal.wait(), 20)
        # The guard must be taken while this holder task is still live, which
        # is the only situation in which production would ever reach it.
        try:
            await session.captured.route_holder.guard_for_python(session.generations.current)
        except RouteCustodyError as exc:
            session.notes["guard_error"] = str(exc)

    with FakeOrigin(tmp_path / "tls", response_mode="complete") as origin:
        async with RealBrowserHarness(origin=origin) as harness:
            session = await harness.arm_route(action=release_to_origin)
            await _hold_and_capture(harness, session)
            await asyncio.wait_for(session.finished.wait(), 25)
            await session.settle()

            assert len(origin.ledger.snapshot()) == 1
            assert session.disposition is RouteDisposition.RELEASED_TO_ORIGIN
            # AC-25 (iv): the Python sender is refused, so no second origin POST.
            assert "released_to_origin" in session.notes["guard_error"]
            assert len(origin.ledger.snapshot()) == 1


@pytest.mark.timeout(180)
async def test_local_abort_is_the_only_resend_safe_terminal(tmp_path):
    """A holder-owned abort from HELD is `aborted`; the origin saw nothing."""

    async def abort_action(session: HeldRouteSession) -> None:
        assert session.holder is not None
        await session.holder.abort()

    with FakeOrigin(tmp_path / "tls") as origin:
        async with RealBrowserHarness(origin=origin) as harness:
            session = await harness.arm_route(action=abort_action)
            await _hold_and_capture(harness, session)
            await asyncio.wait_for(session.finished.wait(), 20)
            await session.settle()

            assert session.disposition is RouteDisposition.ABORTED
            assert origin.ledger.snapshot() == ()

            log = _ledger(tmp_path)
            _record_held(log, session)
            log.record_abandoned_pre_invoke(route_disposition="aborted", page_disposition="closed")
            assert log.record.attempt_state == AttemptState.ABANDONED_PRE_INVOKE.value
            assert log.can_fresh_same_turn_mint() is True


# =====================================================================
# 3. Pre-invocation teardown matrix (AC-25 real-browser failure arms)
# =====================================================================


async def _teardown_action(session: HeldRouteSession) -> None:
    """Runner behaviour on a teardown: observe, then close custody fail-closed.

    Waits briefly for a public request event to write a terminal; if the
    browser is gone before any event arrives, the holder's own ``on_teardown``
    writes ``lost``. Either way the guard is then taken while this task is
    still live, which is the only moment production could reach it.
    """
    assert session.holder is not None
    await session.teardown_injected.wait()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(session.terminal.wait(), 3)
    disposition = await session.holder.on_teardown()
    session.notes["disposition"] = disposition.value
    session.terminal.set()
    try:
        await session.holder.guard_for_python(session.generations.current)
        session.notes["guard_error"] = None
    except RouteCustodyError as exc:
        session.notes["guard_error"] = str(exc)


#: Every way D1 lets the send guard refuse. The arm asserts the guard refused
#: with one of these, not with a particular one: which cut a given teardown
#: trips (dead holder task vs bumped generation) is a Playwright-ordering
#: detail, and pinning one of them would make the arm assert the accident
#: rather than the property. All three are fail-closed and pre-network.
CUSTODY_REFUSALS = (
    "route holder task is not live",
    "page/context/CDP generation changed",
    "not held; Python POST refused",
)


async def _run_teardown_arm(
    tmp_path: Any,
    inject: Any,
    *,
    arm: str,
    cancel_holder: bool = False,
    detached_chromium: bool = False,
) -> dict:
    """One AC-25 pre-invocation arm; returns the evidence row it produced."""
    with FakeOrigin(tmp_path / "tls") as origin:
        async with RealBrowserHarness(
            origin=origin, detached_chromium=detached_chromium
        ) as harness:
            session = await harness.arm_route(action=_teardown_action)
            await _hold_and_capture(harness, session)
            log = _ledger(tmp_path)
            _record_held(log, session)
            assert log.record.attempt_state == AttemptState.REQUEST_HELD.value

            await inject(harness, session)
            session.teardown_injected.set()
            if cancel_holder:
                await harness.inject_worker_cancellation(session)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(session.finished.wait(), 20)
            await session.settle()

            # The holder task is gone in every arm; D1 makes its death write a
            # terminal, so read the disposition after it has been reaped.
            guard_error = session.notes.get("guard_error")
            if cancel_holder:
                # A cancelled worker never reached the in-handler guard call.
                assert session.holder is not None
                with pytest.raises(RouteCustodyError) as excinfo:
                    await session.holder.guard_for_python(session.generations.current)
                guard_error = str(excinfo.value)

            receipts = origin.ledger.snapshot()
            row = {
                "arm": arm,
                "disposition": session.disposition.value,
                "guard_refusal": guard_error,
                "playwright_events": list(session.events),
                "origin_receipts": len(receipts),
                "restart_action": log.restart_action(),
            }

            # AC-25: at most one origin invocation on every arm.
            assert len(receipts) <= 1, receipts
            # No Python POST was ever admitted.
            assert log.record.reserved_at is None
            assert log.record.invoked_at is None

            # The send guard refused, by one of D1's three fail-closed cuts.
            assert guard_error is not None, "the guard admitted a torn-down hold"
            assert any(reason in guard_error for reason in CUSTODY_REFUSALS), guard_error

            # A torn-down hold is never resend-safe: `aborted` is reserved for a
            # holder-owned abort that succeeded from HELD, and nothing upgrades
            # `lost` to it.
            assert session.disposition is not RouteDisposition.ABORTED
            assert session.disposition in {
                RouteDisposition.LOST,
                RouteDisposition.RELEASED_TO_ORIGIN,
            }, session.disposition
            log.record_ack_unknown(
                route_disposition=row["disposition"],
                page_disposition="closed",
            )
            assert log.record.attempt_state == AttemptState.ACK_UNKNOWN.value
            assert log.can_fresh_same_turn_mint() is False
            _record_evidence(row)
            return row


@pytest.mark.timeout(180)
async def test_arm_pre_handler_cut_is_intercept_unproven(tmp_path):
    """Teardown before the handler fires: no hold, no Python POST, no receipt."""
    with FakeOrigin(tmp_path / "tls") as origin:
        async with RealBrowserHarness(origin=origin) as harness:
            session = await harness.arm_route()
            await harness.open_page()
            # The page dies before the composer's fetch can reach the handler.
            await harness.inject_page_close()
            with contextlib.suppress(Exception):
                await harness.trigger_conversation_post()
            await asyncio.sleep(0.3)

            assert not session.entered.is_set()
            assert session.captured is None
            assert session.holder is None
            assert origin.ledger.snapshot() == ()

            log = _ledger(tmp_path)
            # D6: the route could not be proved held -> typed intercept_unproven,
            # never a mint, and the composer's own fate stays unknown.
            assert log.restart_action() == "ack_unknown_reconcile_never_mint"
            log.record_ack_unknown(
                route_disposition=None,
                page_disposition="closed",
                irreconcilable=True,
            )
            assert log.record.attempt_state == AttemptState.ACK_UNKNOWN.value
            assert log.record.route_disposition is None
            assert log.can_fresh_same_turn_mint() is False
            assert log.record.reserved_at is None


@pytest.mark.timeout(180)
async def test_arm_navigation_before_invocation(tmp_path):
    async def inject(harness, _session):
        await harness.inject_navigation()

    row = await _run_teardown_arm(tmp_path, inject, arm="navigate")
    assert row["origin_receipts"] == 0


@pytest.mark.timeout(180)
async def test_arm_page_close_before_invocation(tmp_path):
    async def inject(harness, _session):
        await harness.inject_page_close()

    row = await _run_teardown_arm(tmp_path, inject, arm="page_close")
    assert row["origin_receipts"] == 0


@pytest.mark.timeout(180)
async def test_arm_context_close_before_invocation(tmp_path):
    async def inject(harness, _session):
        await harness.inject_context_close()

    row = await _run_teardown_arm(tmp_path, inject, arm="context_close")
    assert row["origin_receipts"] == 0


@pytest.mark.timeout(180)
async def test_arm_playwright_disconnect_before_invocation(tmp_path):
    async def inject(harness, _session):
        await harness.inject_browser_disconnect()

    row = await _run_teardown_arm(tmp_path, inject, arm="playwright_disconnect")
    assert row["origin_receipts"] == 0


@pytest.mark.timeout(180)
async def test_arm_chromium_kill_before_invocation(tmp_path):
    async def inject(harness, _session):
        await harness.inject_chromium_kill()
        assert not harness.chromium_alive()

    row = await _run_teardown_arm(tmp_path, inject, arm="chromium_kill")
    assert row["origin_receipts"] == 0


@pytest.mark.timeout(180)
async def test_arm_driver_death_chromium_surviving(tmp_path):
    """The node driver dies while Chromium lives; custody is still fail-closed.

    Chromium is spawned by the harness and Playwright attaches over CDP, so
    killing the driver really does leave the browser running; a
    ``chromium.launch()`` browser is the driver's own child and would die with
    it, which is a different arm that ``chromium_kill`` already covers.
    """
    observed: dict = {}

    async def inject(harness, _session):
        await harness.inject_driver_death()
        observed["chromium_alive"] = harness.chromium_alive()

    row = await _run_teardown_arm(tmp_path, inject, arm="driver_death", detached_chromium=True)
    # Whether Chromium releases the paused request when its client vanishes is
    # a browser-owned behaviour; the design only requires that the runner never
    # treats it as proved non-delivery and never issues a second POST.
    assert row["origin_receipts"] in {0, 1}, row
    assert row["disposition"] != RouteDisposition.ABORTED.value
    # The arm is only meaningful if Chromium really outlived its driver.
    assert observed["chromium_alive"] is True, observed


@pytest.mark.timeout(180)
async def test_arm_worker_cancellation_before_invocation(tmp_path):
    async def inject(_harness, _session):
        return None

    row = await _run_teardown_arm(tmp_path, inject, arm="worker_cancellation", cancel_holder=True)
    assert row["origin_receipts"] == 0
