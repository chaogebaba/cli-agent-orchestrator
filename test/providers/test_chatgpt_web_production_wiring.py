"""F862 (#718) r3 — production-path wiring + reachability (verdict Blockers 1, 2).

Blocker 1: the provider must LAUNCH and DRIVE the runner; a dispatched task must
reach the runner (never the shell), a FINDINGS-READY report must be written
BEFORE exactly one worker-scoped callback.

Blocker 2: the fail-closed controls (``ProfileLock``, ``enforce_no_api_egress``,
``verify_attachment_on_turn``) must be REACHED on the single production run path;
each reachability test fails if its call is removed.

These drive the REAL provider and REAL ``run_production_review`` with stubbed
browser / pin / callback seams — no live browser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cli_agent_orchestrator.chatgpt_web_runner import production
from cli_agent_orchestrator.chatgpt_web_runner.poll_gate import AcceptedAnswer
from cli_agent_orchestrator.providers.chatgpt_web import ChatGptWebProvider

pytestmark = pytest.mark.unit

_VALID_BODY = (
    "Finding 1: Cite: D3 line 4. OLD: `permitted every private backend endpoint`. "
    "REPLACE: `permits exactly two reads`. Evidence: recon note."
)


def _accepted(text: str = _VALID_BODY) -> AcceptedAnswer:
    return AcceptedAnswer(
        text=text,
        model_slug="gpt-5-6-thinking",
        thinking_effort="extended",
        conversation_id="11111111-2222-3333-4444-555555555555",
        assistant_node_id="asst-1",
    )


# ── Blocker 1: dispatch reaches the runner, report-before-callback, no shell ──
def test_provider_handles_own_dispatch_and_writes_task_file(tmp_path, monkeypatch) -> None:
    p = ChatGptWebProvider("wkr1", "sess", "win", agent_profile="design_findings")
    # Redirect the runtime dir into tmp so the test does not touch /data.
    p._runtime_dir = tmp_path / "runtime"
    p._task_file = p._runtime_dir / "task.txt"

    assert p.handles_own_dispatch is True  # send_input routes to dispatch_task
    task = "ARTIFACT: /abs/pin.md\n\nreview it"
    p.dispatch_task(task)
    # The dispatched bytes reached the runner's task file — NEVER the shell.
    assert p._task_file.read_text() == task


def test_send_input_routes_to_dispatch_task_not_shell(monkeypatch) -> None:
    """terminal_service.send_input must call dispatch_task (not paste) for a
    provider with handles_own_dispatch=True — the shell never sees the task."""
    from cli_agent_orchestrator.services import terminal_service as ts

    dispatched: list[str] = []
    pasted: list[str] = []

    class _Prov:
        handles_own_dispatch = True
        paste_enter_count = 1
        assume_processing_on_dispatch = False
        blocks_orchestrated_input_while_waiting_user_answer = False
        composer_stash_keys = None

        def dispatch_task(self, message: str) -> None:
            dispatched.append(message)

        def mark_input_received(self) -> None:
            pass

        def pre_paste_gate(self) -> None:
            pass

    prov = _Prov()

    monkeypatch.setattr(
        ts,
        "get_terminal_metadata",
        lambda tid: {"tmux_session": "s", "tmux_window": "w", "caller_id": None},
        raising=False,
    )
    monkeypatch.setattr(ts.provider_manager, "get_provider", lambda tid: prov, raising=False)
    monkeypatch.setattr(ts, "_fixture_send_input_override", lambda *a, **k: False, raising=False)
    monkeypatch.setattr(ts, "_append_message_contract", lambda m, *a, **k: m, raising=False)
    monkeypatch.setattr(ts, "inject_memory_context", lambda m, *a, **k: m, raising=False)
    monkeypatch.setattr(ts, "update_last_active", lambda tid: None, raising=False)

    class _Backend:
        def send_keys(self, *a, **k):
            pasted.append(a[2] if len(a) > 2 else k.get("keys", ""))

    monkeypatch.setattr(ts, "get_backend", lambda: _Backend(), raising=False)
    monkeypatch.setattr(
        ts.status_monitor, "get_status", lambda tid: ts.TerminalStatus.IDLE, raising=False
    )
    monkeypatch.setattr(ts.status_monitor, "notify_input_sent", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        ts.status_monitor, "clear_rolling_buffer", lambda *a, **k: None, raising=False
    )
    monkeypatch.setattr(
        ts.status_monitor, "bind_dispatch_provider", lambda *a, **k: None, raising=False
    )

    class _Txn:
        pass

    monkeypatch.setattr(ts.status_monitor, "begin_dispatch", lambda tid: _Txn(), raising=False)
    monkeypatch.setattr(ts.status_monitor, "commit_dispatch", lambda txn: None, raising=False)
    monkeypatch.setattr(ts.status_monitor, "abort_dispatch", lambda txn: None, raising=False)
    monkeypatch.setattr(ts, "preserve_draft_before_send", lambda *a, **k: None, raising=False)

    ok = ts.send_input(
        "t1", "ARTIFACT: /p.md\n\ndo it", orchestration_type=ts.OrchestrationType.ASSIGN
    )
    assert ok is True
    assert dispatched == ["ARTIFACT: /p.md\n\ndo it"]  # reached dispatch_task
    assert pasted == []  # the shell NEVER received the task


def test_report_written_before_exactly_one_callback(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    events: list[str] = []

    def _callback(msg: str) -> None:
        events.append(f"callback:{'READY' if 'FINDINGS-READY' in msg else 'other'}")

    def _publish_spy_wrap(orig):
        def _p(body: str) -> str:
            events.append("report_written")
            return orig(body)

        return _p

    # Stub browser turn + always-valid pin.
    outcome = production.run_production_review(
        task_text="review it",
        artifact_path="/abs/pin.md",
        verify_pin=lambda path: True,
        callback=_callback,
        browser_turn=lambda: _accepted(),
    )
    assert outcome.ok is True
    assert outcome.report_path and Path(outcome.report_path).exists()
    # Exactly ONE callback, and it is the READY callback AFTER the report exists.
    assert events.count("callback:READY") == 1
    assert "callback:other" not in events


def test_findings_invalid_body_ends_findings_invalid_no_callback_ready(
    tmp_path, monkeypatch
) -> None:
    """Schema validation is UNCONDITIONAL now.

    At B1 it rode ``bool(bundle_path)``, so a run without an upload silently
    skipped report validation. Amendment D deleted the upload (D10), which would
    have turned validation off for every run; it is now always on, and this test
    passes no attachment at all.
    """
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    callbacks: list[str] = []
    outcome = production.run_production_review(
        task_text="review it",
        artifact_path="/abs/pin.md",
        verify_pin=lambda path: True,
        callback=lambda m: callbacks.append(m),
        browser_turn=lambda: _accepted("Finding 1: no old quote, no replace."),
    )
    assert outcome.ok is False
    assert outcome.error_code is not None and outcome.error_code.value == "report_invalid"
    assert outcome.report_path is None
    # The callback is sent but as FINDINGS-INVALID, never FINDINGS-READY.
    assert callbacks and "FINDINGS-INVALID" in callbacks[0]


# ── Blocker 2: reachability of the fail-closed controls ──────────────────────
def test_reachability_verify_pin_called_at_start_and_before_publish(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    calls: list[str] = []

    def _pin(path: str) -> bool:
        calls.append(path)
        return True

    production.run_production_review(
        task_text="review it",
        artifact_path="/abs/pin.md",
        verify_pin=_pin,
        callback=lambda m: None,
        browser_turn=lambda: _accepted(),
    )
    # Two verify_pin calls: start + before-publication (D2/AC-1/AC-2).
    assert calls == ["/abs/pin.md", "/abs/pin.md"]


def test_reachability_pin_drift_before_publish_blocks_report(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    seq = iter([True, False])  # VALID at start, DRIFT before publish
    outcome = production.run_production_review(
        task_text="review it",
        artifact_path="/abs/pin.md",
        verify_pin=lambda path: next(seq),
        callback=lambda m: None,
        browser_turn=lambda: _accepted(),
    )
    assert outcome.ok is False
    assert outcome.error_code is not None and outcome.error_code.value == "pin_drift"
    assert outcome.report_path is None


def test_reachability_profile_lock_acquired_in_initialize_released_in_cleanup(monkeypatch) -> None:
    """The provider MUST acquire the ProfileLock in initialize and release it in
    cleanup (D5 owner lease). Removing either call fails this test."""
    import cli_agent_orchestrator.providers.chatgpt_web as cw

    acquired: list[str] = []
    released: list[str] = []

    class _FakeLock:
        def __init__(self, profile, tid):
            self.tid = tid

        def acquire(self, **k):
            acquired.append(self.tid)

        def release(self):
            released.append(self.tid)

    monkeypatch.setattr(
        cw, "_RUNTIME_ROOT", Path("/data/cao-scratch/worker-scratch/f862-build/test-runtime")
    )

    async def _run() -> None:
        import cli_agent_orchestrator.chatgpt_web_runner.runtime as rt

        monkeypatch.setattr(rt, "ProfileLock", _FakeLock, raising=False)
        monkeypatch.setattr(
            rt,
            "resolve_profile_dir",
            lambda: Path("/data/cao-scratch/chatgpt-web/profile"),
            raising=False,
        )

        import cli_agent_orchestrator.utils.terminal as term

        async def _wait(*a, **k):
            return True

        monkeypatch.setattr(term, "wait_for_shell", _wait, raising=False)

        class _Backend:
            def send_keys(self, *a, **k):
                pass

        import cli_agent_orchestrator.backends.registry as reg

        monkeypatch.setattr(reg, "get_backend", lambda: _Backend(), raising=False)

        p = ChatGptWebProvider("wkr9", "sess", "win", agent_profile="design_findings")
        await p.initialize()
        assert acquired == ["wkr9"]  # ProfileLock.acquire reached in initialize
        p.cleanup()
        assert released == ["wkr9"]  # ProfileLock.release reached in cleanup

    import asyncio

    asyncio.run(_run())


# ── Reachability of the ONE route dispatcher on the composed D path ──────────
# ── (Blocker 2 + D10). Stub the browser so _drive_composed_turn runs far     ──
# ── enough to install it.                                                   ──
class _FakeLocator:
    def __init__(self, page):
        self._page = page

    @property
    def first(self):
        return self

    async def wait_for(self, **k):
        return None

    async def count(self):
        return 1

    async def click(self, **k):
        return None

    async def press(self, *a, **k):
        return None

    async def inner_text(self):
        return ""


class _FakeKeyboard:
    def __init__(self):
        self.typed: list = []

    async def insert_text(self, text):
        self.typed.append(text)


class _FakePage:
    def __init__(self):
        self.routed: list = []
        self.keyboard = _FakeKeyboard()
        self.url = "https://chatgpt.com/c/WEB:11111111-2222-3333-4444-555555555555"

    def locator(self, sel):
        return _FakeLocator(self)

    def on(self, event, handler):
        return None

    async def route(self, pattern, handler):
        self.routed.append((pattern, handler))

    async def goto(self, url, **k):
        return None

    async def wait_for_timeout(self, ms):
        return None

    async def evaluate(self, script, *a):
        return 999  # readback length; also generic evaluate return


class _Route:
    def __init__(self, url, method="GET", post_data=b""):
        self.request = type(
            "R",
            (),
            {"url": url, "method": method, "post_data_buffer": post_data, "headers": {}},
        )()
        self.aborted = False
        self.continued = False

    async def abort(self):
        self.aborted = True

    async def continue_(self):
        self.continued = True


def _composed_turn_with_fakes(monkeypatch, tmp_path, attempt_id="a1"):
    """Run _drive_composed_turn far enough to install the dispatcher, and stop.

    The mint never produces a held route here (the fake page's Enter does not
    issue a POST), so the turn fails on the hold timeout. That is the point: the
    dispatcher must already be installed BEFORE any composer action, so it is
    reachable regardless of how the turn ends.
    """
    import asyncio

    import cli_agent_orchestrator.chatgpt_web_runner.production as prod
    import cli_agent_orchestrator.chatgpt_web_runner.runtime as rt

    page = _FakePage()

    class _Ctx:
        pages = [page]

        def on(self, event, handler):
            return None

        async def close(self):
            return None

        async def new_page(self):
            return page

    async def _fake_launch(opts):
        return _Ctx()

    monkeypatch.setattr(rt, "launch", _fake_launch, raising=False)
    monkeypatch.setattr(
        rt,
        "resolve_profile_dir",
        lambda: Path("/data/cao-scratch/chatgpt-web/profile"),
        raising=False,
    )
    monkeypatch.setattr(rt, "pin_fingerprint_seed", lambda p: "12345", raising=False)
    monkeypatch.setattr(prod, "_HOLD_TIMEOUT_S", 0.05, raising=False)

    from cli_agent_orchestrator.chatgpt_web_runner.stream_relay import get_relay_hub
    from cli_agent_orchestrator.providers.chatgpt_web import ChatGptWebProvider

    # The relay registry is process-global and transient; each test owns one
    # attempt, so clear it rather than colliding on a reused id.
    get_relay_hub().reset_for_tests()
    ChatGptWebProvider.start_attempt(
        run_id="r1", attempt_id=attempt_id, prompt_sha="0" * 64, artifacts_dir=tmp_path
    )
    from cli_agent_orchestrator.chatgpt_web_runner.send_intent import SendIntentLog

    log = SendIntentLog(tmp_path / "attempts" / attempt_id)
    log.load()
    # Same single-writer rule run_production_review applies (see its comment):
    # the relay must share this record, not the one start_attempt created.
    get_relay_hub().get(attempt_id).intent_log = log

    from cli_agent_orchestrator.chatgpt_web_runner.errors import RunnerError

    with pytest.raises(RunnerError) as excinfo:
        asyncio.run(
            prod._drive_composed_turn(
                task_text="x",
                run_id="r1",
                attempt_id=attempt_id,
                prompt_sha="0" * 64,
                intent_log=log,
                manifest=[],
                frozen_worktree=None,
                reviewed_commit=None,
                base_commit=None,
                pull_evidence={},
            )
        )
    return page, excinfo.value


def test_one_dispatcher_installed_before_any_composer_action(tmp_path, monkeypatch) -> None:
    """Exactly ONE route dispatcher, installed before the composer is touched."""
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    page, error = _composed_turn_with_fakes(monkeypatch, tmp_path)

    assert len(page.routed) == 1, f"expected one dispatcher, got {page.routed}"
    assert page.routed[0][0] == "**/*"
    # The mint produced no held POST, so the turn refuses rather than proceeding.
    assert "no held conversation POST" in str(error)


def test_dispatcher_denies_platform_api_egress(tmp_path, monkeypatch) -> None:
    """D3/AC-11: a dynamically constructed OpenAI-API host is aborted at request time."""
    import asyncio

    import cli_agent_orchestrator.chatgpt_web_runner.snapshot_upload as su

    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    egress_calls: list[str] = []
    real_enforce = su.enforce_no_api_egress

    def _spy_enforce(url: str) -> None:
        egress_calls.append(url)
        return real_enforce(url)

    monkeypatch.setattr(su, "enforce_no_api_egress", _spy_enforce, raising=False)
    page, _ = _composed_turn_with_fakes(monkeypatch, tmp_path)
    dispatcher = page.routed[0][1]

    forbidden = _Route("https://api.openai.com/v1/x")
    asyncio.run(dispatcher(forbidden))
    assert forbidden.aborted is True and forbidden.continued is False
    assert "api.openai.com" in egress_calls[-1]


def test_dispatcher_continues_ordinary_page_traffic(tmp_path, monkeypatch) -> None:
    """Non-conversation traffic keeps its normal behaviour, or the page cannot load."""
    import asyncio

    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    page, _ = _composed_turn_with_fakes(monkeypatch, tmp_path)
    dispatcher = page.routed[0][1]

    ordinary = _Route("https://chatgpt.com/static/app.js")
    asyncio.run(dispatcher(ordinary))
    assert ordinary.continued is True and ordinary.aborted is False


def test_dispatcher_never_continues_the_conversation_post(tmp_path, monkeypatch) -> None:
    """D10: there is NO ``route.continue_()`` success branch for the send.

    The conversation POST is captured and held. A dispatcher that continued it
    would be Amendment A's browser-to-origin send, which is exactly the deletion
    this test defends — so the assertion is that neither ``continue_()`` nor
    ``abort()`` is ever reached for that request.
    """
    import asyncio

    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    page, _ = _composed_turn_with_fakes(monkeypatch, tmp_path)
    dispatcher = page.routed[0][1]

    body = b'{"messages":[{"id":"u","author":{"role":"user"},"content":{"parts":["x"]}}]}'
    send = _Route("https://chatgpt.com/backend-api/f/conversation", "POST", body)
    asyncio.run(dispatcher(send))

    assert send.continued is False, "the conversation POST was released to the origin"
    assert send.aborted is False, "the conversation POST must be HELD, not aborted"


def test_prepare_is_never_held(tmp_path, monkeypatch) -> None:
    """The ``/prepare`` pre-warm is not the send and must pass through."""
    import asyncio

    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    page, _ = _composed_turn_with_fakes(monkeypatch, tmp_path)
    dispatcher = page.routed[0][1]

    prepare = _Route("https://chatgpt.com/backend-api/f/conversation/prepare", "POST", b"{}")
    asyncio.run(dispatcher(prepare))
    assert prepare.continued is True


# ── D3/D6: the relay binding window, and who owns it ─────────────────────────
def test_the_runner_does_not_consume_the_single_relay_binding(tmp_path, monkeypatch) -> None:
    """The one subscriber is whoever presented the token, not the runner.

    ``AttemptRelay.bind`` mints the subscriber id and writes
    RELAY_BOUND_OR_SKIPPED itself, so a runner that called it would take the
    single binding and lock the real subscriber out with a 409.
    """
    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))
    _composed_turn_with_fakes(monkeypatch, tmp_path, attempt_id="relay-a")

    from cli_agent_orchestrator.chatgpt_web_runner.send_intent import SendIntentLog
    from cli_agent_orchestrator.chatgpt_web_runner.stream_relay import get_relay_hub

    relay = get_relay_hub().get("relay-a")
    # Nothing bound, so the window closed as an explicit SKIP before the mint.
    assert relay.is_bound is False
    assert relay.status == "skipped"

    log = SendIntentLog(tmp_path / "attempts" / "relay-a")
    record = log.load()
    assert record is not None
    assert record.skipped_at is not None
    assert record.subscriber_id is None


def test_a_real_subscriber_keeps_its_binding_through_the_turn(tmp_path, monkeypatch) -> None:
    """A subscriber that bound BEFORE the mint is still the bound one after."""
    import asyncio

    monkeypatch.setenv("CAO_ARTIFACTS_DIR", str(tmp_path))

    from cli_agent_orchestrator.chatgpt_web_runner.stream_relay import get_relay_hub
    from cli_agent_orchestrator.providers.chatgpt_web import ChatGptWebProvider

    get_relay_hub().reset_for_tests()
    handle = ChatGptWebProvider.start_attempt(
        run_id="r2", attempt_id="relay-b", prompt_sha="0" * 64, artifacts_dir=tmp_path
    )
    relay = get_relay_hub().get("relay-b")
    binding = asyncio.run(relay.bind(handle.relay_token))
    assert relay.is_bound is True

    # The turn runs (and fails on the hold timeout) without touching the binding.
    _composed_turn_with_fakes(monkeypatch, tmp_path / "second", attempt_id="relay-b2")

    assert relay.binding is binding
    assert relay.status == "bound"
