"""D11 build-stop arms that DRIVE the composed path (B3 review fix 1).

The arms this module replaces were source-text scans of ``production.py``: they
asserted the build-stop branch was written, which it was, while the control flow
could not reach it on either ordering. These drive the real
``_drive_composed_turn`` and assert what the supervisor is told to read — the
disposition, the ledger row and the callback.

Both D11 orderings are covered:

* **A** — a ``response`` observed while the route is still HELD terminates
  ``released_to_origin`` (D1) BEFORE ``fulfil()`` is called. ``fulfil()`` refuses
  from a non-HELD disposition, so the old code raised ``RouteCustodyError`` (a
  RuntimeError, which ``run_review`` does not catch) and skipped the detector,
  the ledger write and the callback entirely.
* **B** — the event lands at or after ``fulfil()``'s HELD->FULFILLING CAS. By
  D1(iii) that is indistinguishable from the local fulfil's own completion and
  terminates ``fulfilled``. That classification is correct and is NOT
  reinterpreted; what is fixed is that the outcome is now RECORDED, so
  ``route_disposition`` separates the outcomes instead of being absent either way.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from test.fixtures import chatgpt_web_composed_turn as harness

import pytest

from cli_agent_orchestrator.chatgpt_web_runner import production
from cli_agent_orchestrator.chatgpt_web_runner.errors import RunnerError, RunnerErrorCode
from cli_agent_orchestrator.chatgpt_web_runner.send_intent import AttemptState, SendIntentLog

pytestmark = pytest.mark.unit


def _attempt(tmp_path: Path, attempt_id: str) -> SendIntentLog:
    from cli_agent_orchestrator.chatgpt_web_runner.stream_relay import get_relay_hub
    from cli_agent_orchestrator.providers.chatgpt_web import ChatGptWebProvider

    get_relay_hub().reset_for_tests()
    ChatGptWebProvider.start_attempt(
        run_id="run-d11", attempt_id=attempt_id, prompt_sha="0" * 64, artifacts_dir=tmp_path
    )
    log = SendIntentLog(tmp_path / "attempts" / attempt_id)
    log.load()
    get_relay_hub().get(attempt_id).intent_log = log
    return log


def _plain_ledger(tmp_path: Path, attempt_id: str) -> SendIntentLog:
    """A ledger already at PYTHON_POST_INVOKED, for the classifier arms."""
    import time

    log = SendIntentLog(tmp_path / attempt_id)
    log.open_attempt(
        run_id="r", attempt_id=attempt_id, prompt_sha="0" * 64, deadline_at=time.time() + 600
    )
    return log


def _drive(log: SendIntentLog, attempt_id: str):
    return production._drive_composed_turn(
        task_text="review it\n\nEND_REVIEW:run-d11:" + "0" * 64,
        run_id="run-d11",
        attempt_id=attempt_id,
        prompt_sha="0" * 64,
        intent_log=log,
        manifest=[],
        frozen_worktree=None,
        reviewed_commit=None,
        base_commit=None,
        pull_evidence={},
    )


# =====================================================================
# Ordering A — the release is recorded BEFORE fulfil is attempted
# =====================================================================


def test_ordering_a_release_before_fulfil_is_a_typed_build_stop(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    page = harness.FakePage()
    harness.install(monkeypatch, page)
    # The browser's copy reaches the origin while the route is still HELD.
    harness.install_sender(monkeypatch, before_return=lambda: page.emit("response"))
    harness.install_get(monkeypatch)
    log = _attempt(tmp_path, "d11-a")

    with pytest.raises(RunnerError) as excinfo:
        asyncio.run(_drive(log, "d11-a"))

    # TYPED, not a bare RuntimeError: this is what run_review can catch.
    assert excinfo.value.code is RunnerErrorCode.SUBMIT_UNKNOWN
    assert "D11 build stop" in str(excinfo.value.hint)
    assert "before the local fulfil" in str(excinfo.value.hint)

    # The ledger row the live-turn ledger tells the supervisor to read.
    record = SendIntentLog(tmp_path / "attempts" / "d11-a").load()
    assert record is not None
    assert record.route_disposition == "released_to_origin"
    assert record.attempt_state == AttemptState.ACK_UNKNOWN.value

    # And nothing was published: the browser copy was never locally fulfilled.
    assert page.conversation_route is not None
    assert page.conversation_route.fulfilled == []


def test_ordering_a_sends_a_failure_callback(tmp_path, monkeypatch) -> None:
    """The escape the review found: no callback at all, of either kind."""
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    page = harness.FakePage()
    harness.install(monkeypatch, page)
    harness.install_sender(monkeypatch, before_return=lambda: page.emit("response"))
    harness.install_get(monkeypatch)

    callbacks: list[str] = []
    outcome = production.run_production_review(
        task_text="review it",
        artifact_path="/abs/pin.md",
        verify_pin=lambda _p: True,
        callback=callbacks.append,
    )

    assert outcome.ok is False
    assert outcome.report_path is None
    # EXACTLY ONE callback, and it says the run failed.
    assert len(callbacks) == 1, callbacks
    assert "FINDINGS-FAILED" in callbacks[0]
    assert "FINDINGS-READY" not in callbacks[0]


# =====================================================================
# Ordering B — the event lands at/after fulfil's CAS
# =====================================================================


def test_ordering_b_event_during_fulfil_is_recorded_as_fulfilled(tmp_path, monkeypatch) -> None:
    """D1(iii) classification is preserved AND written down."""
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    page = harness.FakePage()
    harness.install(monkeypatch, page)
    harness.install_sender(monkeypatch)
    harness.install_get(monkeypatch)
    log = _attempt(tmp_path, "d11-b")

    def _arm_fulfil_event() -> None:
        assert page.conversation_route is not None
        page.conversation_route.on_fulfill = lambda: page.emit("response")

    # Arm it once the route exists, i.e. after the mint.
    original = page.issue_conversation_post

    async def _issue() -> None:
        await original()
        _arm_fulfil_event()

    page.issue_conversation_post = _issue  # type: ignore[method-assign]

    answer = asyncio.run(_drive(log, "d11-b"))
    assert answer.assistant_node_id == "asst-fake-1"

    record = SendIntentLog(tmp_path / "attempts" / "d11-b").load()
    assert record is not None
    # The outcome is `fulfilled` (D1(iii)) and it is now RECORDED, so "absent"
    # can no longer mean both "clean run" and "the detector never ran".
    assert record.route_disposition == "fulfilled"
    assert record.attempt_state == AttemptState.BROWSER_FULFIL.value
    assert record.fulfilled_at is not None
    assert page.conversation_route is not None
    assert len(page.conversation_route.fulfilled) == 1


# =====================================================================
# The happy path, so the arms above are not vacuous
# =====================================================================


def test_the_clean_path_records_all_four_ledger_fields(tmp_path, monkeypatch) -> None:
    """The three §6 fields that had no writer, plus the explicit disposition."""
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    page = harness.FakePage()
    harness.install(monkeypatch, page)
    harness.install_sender(monkeypatch)
    harness.install_get(monkeypatch, branch_digest="sha256:branch-clean")
    log = _attempt(tmp_path, "d11-clean")

    answer = asyncio.run(_drive(log, "d11-clean"))
    assert answer.text.startswith("Finding 1")

    record = SendIntentLog(tmp_path / "attempts" / "d11-clean").load()
    assert record is not None
    assert record.verified_node_id == "asst-fake-1"
    assert record.conversation_digest == "sha256:branch-clean"
    assert record.fulfilled_at is not None
    assert record.route_disposition == "fulfilled"
    assert record.attempt_state == AttemptState.BROWSER_FULFIL.value
    # The counters the live-turn ledger reads.
    assert record.submits_dispatched == 1
    assert record.sends_observed == 1
    assert record.recoveries_used == 0
    assert record.reserved_at is not None
    assert record.invoked_at is not None


def test_the_clean_path_writes_a_readable_json_record(tmp_path, monkeypatch) -> None:
    """The supervisor reads send_intent.json, not a Python object."""
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    page = harness.FakePage()
    harness.install(monkeypatch, page)
    harness.install_sender(monkeypatch)
    harness.install_get(monkeypatch)
    log = _attempt(tmp_path, "d11-json")
    asyncio.run(_drive(log, "d11-json"))

    body = json.loads((tmp_path / "attempts" / "d11-json" / "send_intent.json").read_text())
    for field in ("verified_node_id", "conversation_digest", "fulfilled_at", "route_disposition"):
        assert body.get(field) is not None, f"{field} is null in the record the supervisor reads"


# =====================================================================
# The handler PARKS (B3 review fix 3)
# =====================================================================


def test_the_production_handler_parks_inside_a_live_custody_window(tmp_path, monkeypatch) -> None:
    """Every previous dispatcher test ran AFTER custody.release was set.

    ``_composed_turn_with_fakes`` drives the turn to completion, and its
    ``finally`` sets ``custody.release`` — so ``await custody.release.wait()``
    returned immediately and the parking behaviour, which is the whole point of
    ``_HeldRouteCustody`` and the holder-task lifetime contract, was never
    exercised. Here the handler is inspected while the window is still open.
    """
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    page = harness.FakePage()
    harness.install(monkeypatch, page)
    harness.install_get(monkeypatch)
    log = _attempt(tmp_path, "d11-park")

    observed: dict[str, object] = {}

    def _inspect() -> None:
        # Called from inside send_once, i.e. mid-custody-window.
        task = page.dispatcher_task
        assert task is not None
        observed["pending"] = not task.done()
        route = page.conversation_route
        assert route is not None
        observed["continued"] = route.continued
        observed["aborted"] = route.aborted
        observed["fulfilled"] = list(route.fulfilled)

    harness.install_sender(monkeypatch, before_return=_inspect)
    asyncio.run(_drive(log, "d11-park"))

    # The handler task was STILL RUNNING while Python held the mint: it parked
    # rather than returning, so the route's owner task stayed alive and the
    # holder did not classify its own live route `lost`.
    assert observed["pending"] is True, "the dispatcher returned while the route was held"
    assert observed["continued"] is False
    assert observed["aborted"] is False
    assert observed["fulfilled"] == []

    # And it ended once the window closed.
    assert page.dispatcher_task is not None and page.dispatcher_task.done()


def test_a_returning_handler_would_lose_the_route(tmp_path, monkeypatch) -> None:
    """Why parking is load-bearing, stated as an executable fact.

    If the handler returned instead of parking, the owner task completes and
    ``HeldRoute`` writes the fail-closed ``lost`` terminal — which then refuses
    the Python POST. This is the contract ``capture_held_route`` documents.
    """
    from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import (
        RouteCustodyError,
        RouteDisposition,
    )
    from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import (
        RouteGenerations as _Gens,
    )
    from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import (
        capture_held_route,
    )

    gens = _Gens(page=1, context=1, cdp_session=1)
    route = harness.FakeRoute(harness.CONVERSATION_URL, "POST", harness.CONVERSATION_BODY)

    async def _run():
        captured = await capture_held_route(
            route, attempt_id="a", mint_id="m", profile_epoch="e", generations=gens
        )
        return captured

    async def _handler_that_returns():
        return await _run()

    async def _drive_it():
        task = asyncio.ensure_future(_handler_that_returns())
        captured = await task
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return captured

    captured = asyncio.run(_drive_it())
    assert captured.route_holder.disposition is RouteDisposition.LOST

    async def _guard():
        await captured.route_holder.guard_for_python(gens)

    with pytest.raises(RouteCustodyError, match="not live"):
        asyncio.run(_guard())


# =====================================================================
# Ordering A' — the release lands BETWEEN the check and the CAS
# =====================================================================


def _released_holder():
    """A holder whose route was released to the origin, for the classifier arms."""
    from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import (
        HeldRoute,
        RouteDisposition,
        RouteGenerations,
    )

    gens = RouteGenerations(page=1, context=1, cdp_session=1)

    class _Route:
        async def fulfill(self, **_kwargs):
            return None

        async def abort(self):
            return None

    async def _live() -> None:
        await asyncio.sleep(3600)

    async def _build(target: str):
        owner = asyncio.ensure_future(_live())
        holder = HeldRoute(_Route(), object(), attempt_id="a", generations=gens, owner_task=owner)
        if target == "released":
            assert await holder.observe("response") is RouteDisposition.RELEASED_TO_ORIGIN
        else:
            assert await holder.on_teardown() is RouteDisposition.LOST
        return holder, owner

    return _build


def test_a_refused_fulfil_on_a_released_route_is_the_d11_stop(tmp_path) -> None:
    """Ordering A', the narrow race: released between the check and the CAS.

    The window cannot be driven deterministically from the harness — the observe
    that closes it is a task and production has no await between the check and
    the CAS — so the branch is arm'd through the named classifier the production
    except-clause delegates to, with a genuinely released holder.
    """
    from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import RouteCustodyError

    log = _plain_ledger(tmp_path, "classify-released")
    build = _released_holder()

    async def _run():
        holder, owner = await build("released")
        try:
            return production.classify_refused_fulfil(
                log, holder, RouteCustodyError("cannot fulfil from released_to_origin")
            )
        finally:
            owner.cancel()

    error = asyncio.run(_run())
    assert isinstance(error, RunnerError)
    assert error.code is RunnerErrorCode.SUBMIT_UNKNOWN
    assert "D11 build stop" in str(error.hint)
    assert "the local fulfil was refused" in str(error.hint)
    assert log.record.route_disposition == "released_to_origin"
    assert log.record.attempt_state == AttemptState.ACK_UNKNOWN.value


def test_a_refused_fulfil_on_a_lost_route_is_typed_but_not_the_d11_stop(tmp_path) -> None:
    """A `lost` route is ack-unknown, not two sends for one mint."""
    from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import RouteCustodyError

    log = _plain_ledger(tmp_path, "classify-lost")
    build = _released_holder()

    async def _run():
        holder, owner = await build("lost")
        try:
            return production.classify_refused_fulfil(
                log, holder, RouteCustodyError("cannot fulfil from lost")
            )
        finally:
            owner.cancel()

    error = asyncio.run(_run())
    assert error.code is RunnerErrorCode.SUBMIT_UNKNOWN
    assert "D11 build stop" not in str(error.hint)
    assert "could not be fulfilled locally" in str(error.hint)
    assert log.record.route_disposition == "lost"
    assert log.record.attempt_state == AttemptState.ACK_UNKNOWN.value


# =====================================================================
# The backstop — no RouteCustodyError may escape the composed turn
# =====================================================================


def test_a_custody_error_from_the_sender_is_typed_not_escaped(tmp_path, monkeypatch) -> None:
    """``send_once``'s own guards raise RouteCustodyError too.

    ``run_review`` catches only ``RunnerError``, so any custody error that
    escapes ``_drive_composed_turn`` escapes the whole runner and the worker
    sends no callback of either kind. The sites that can name a cause classify
    it; the backstop exists for the ones that cannot.
    """
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    page = harness.FakePage()
    harness.install(monkeypatch, page)
    harness.install_get(monkeypatch)
    log = _attempt(tmp_path, "d11-backstop")

    import cli_agent_orchestrator.chatgpt_web_runner.api_drive as api_drive
    from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import RouteCustodyError

    async def _send_once(_captured: object, **_kwargs: object) -> object:
        raise RouteCustodyError("page/context/CDP generation changed")

    monkeypatch.setattr(api_drive, "send_once", _send_once, raising=False)

    with pytest.raises(RunnerError) as excinfo:
        asyncio.run(_drive(log, "d11-backstop"))

    assert excinfo.value.code is RunnerErrorCode.SUBMIT_UNKNOWN
    assert "route custody was lost" in str(excinfo.value.hint)

    record = SendIntentLog(tmp_path / "attempts" / "d11-backstop").load()
    assert record is not None
    assert record.attempt_state == AttemptState.ACK_UNKNOWN.value
    assert record.route_disposition is not None
