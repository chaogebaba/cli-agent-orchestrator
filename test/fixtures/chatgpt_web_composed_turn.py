"""Drive the REAL ``_drive_composed_turn`` offline, to a chosen D11 ordering.

The B3 review's finding was that the D11 build stop had no reachable code path,
and that the arms missed it because two of the three "production" D11 tests were
source-text scans of ``production.py`` — they asserted the branch was *written*,
which it was. Nothing drove production to a release.

This harness closes that. It runs the production function itself, with only the
outermost seams faked: the browser (a page that issues one conversation POST
when Enter is pressed), the origin sender and the authoritative GET. Everything
between — the dispatcher, ``capture_held_route``, ``HeldRoute``, the custody
parking, ``_wire_request_events``, the disposition checks, the ledger writes — is
production code.

No browser, no chatgpt.com, no logged-in profile.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

CONVERSATION_URL = "https://chatgpt.com/backend-api/f/conversation"

#: A body the production rewriter accepts: one user message with content.parts.
CONVERSATION_BODY = (
    b'{"action":"next","messages":[{"id":"browser-user-1","author":{"role":"user"},'
    b'"content":{"content_type":"text","parts":["typed in the composer"]}}],'
    b'"parent_message_id":"node-0"}'
)


class FakeRoute:
    """One intercepted request. Records what production did to it."""

    def __init__(self, url: str, method: str = "GET", body: bytes = b"") -> None:
        self.request = FakeRequest(url, method, body)
        self.aborted = False
        self.continued = False
        self.fulfilled: list[bytes] = []
        #: Fired from inside ``fulfill()``, i.e. at/after the HELD->FULFILLING
        #: CAS — the second D11 ordering.
        self.on_fulfill: Optional[Callable[[], None]] = None

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True

    async def fulfill(self, *, status: int = 200, headers: Any = None, body: bytes = b"") -> None:
        self.fulfilled.append(body)
        if self.on_fulfill is not None:
            self.on_fulfill()


class FakeRequest:
    def __init__(self, url: str, method: str, body: bytes) -> None:
        self.url = url
        self.method = method
        self.post_data_buffer = body
        self.headers = {"content-type": "application/json", "cookie": "session=abc"}


class FakeLocator:
    def __init__(self, page: "FakePage") -> None:
        self._page = page

    @property
    def first(self) -> "FakeLocator":
        return self

    async def wait_for(self, **_kwargs: Any) -> None:
        return None

    async def click(self, **_kwargs: Any) -> None:
        return None

    async def count(self) -> int:
        return 1

    async def inner_text(self) -> str:
        return ""

    async def press(self, *_args: Any, **_kwargs: Any) -> None:
        """Enter: the page issues its conversation POST, exactly once."""
        await self._page.issue_conversation_post()


class FakeKeyboard:
    def __init__(self) -> None:
        self.typed: list[str] = []

    async def insert_text(self, text: str) -> None:
        self.typed.append(text)


class FakePage:
    """A page that routes, emits request events, and mints on Enter."""

    def __init__(self) -> None:
        self.keyboard = FakeKeyboard()
        self.url = "https://chatgpt.com/"
        self.handlers: list[tuple[str, Any]] = []
        self.dispatcher: Optional[Any] = None
        self.conversation_route: Optional[FakeRoute] = None
        self.dispatcher_task: "Optional[asyncio.Task[Any]]" = None

    # --- Playwright surface -------------------------------------------------

    def locator(self, _selector: str) -> FakeLocator:
        return FakeLocator(self)

    def on(self, event: str, handler: Any) -> None:
        self.handlers.append((event, handler))

    async def route(self, _pattern: str, handler: Any) -> None:
        self.dispatcher = handler

    async def goto(self, _url: str, **_kwargs: Any) -> None:
        return None

    async def wait_for_timeout(self, _ms: float) -> None:
        await asyncio.sleep(0)

    async def evaluate(self, _script: str, *_args: Any) -> int:
        return 10_000  # readback length: far above the 60% floor

    # --- the mint -----------------------------------------------------------

    async def issue_conversation_post(self) -> None:
        """Hand the conversation POST to production's dispatcher and let it park.

        The dispatcher never returns while the route is held, so it MUST run as
        its own task — awaiting it here would deadlock the turn, which is also
        what makes it a real parking test.
        """
        assert self.dispatcher is not None, "Enter pressed before the dispatcher was installed"
        route = FakeRoute(CONVERSATION_URL, "POST", CONVERSATION_BODY)
        self.conversation_route = route
        self.dispatcher_task = asyncio.ensure_future(self.dispatcher(route))
        for _ in range(200):
            await asyncio.sleep(0)
            if self.dispatcher_task.done():
                await self.dispatcher_task  # surface a handler exception
                return

    def emit(self, event: str) -> None:
        """Fire a public Playwright request event for the held request."""
        assert self.conversation_route is not None
        for name, handler in self.handlers:
            if name == event:
                handler(self.conversation_route.request)


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]
        self.closed = False

    def on(self, _event: str, _handler: Any) -> None:
        return None

    async def new_page(self) -> FakePage:
        return self.pages[0]

    async def close(self) -> None:
        self.closed = True


def install(monkeypatch: Any, page: FakePage) -> None:
    """Patch only the outermost seams: launch, profile, sender, GET."""
    import cli_agent_orchestrator.chatgpt_web_runner.runtime as runtime

    context = FakeContext(page)

    async def _launch(_options: Any) -> FakeContext:
        return context

    monkeypatch.setattr(runtime, "launch", _launch, raising=False)
    monkeypatch.setattr(runtime, "resolve_profile_dir", lambda: "/data/fake/profile", raising=False)
    monkeypatch.setattr(runtime, "pin_fingerprint_seed", lambda _p: "epoch-fake", raising=False)


def install_sender(
    monkeypatch: Any,
    *,
    before_return: Optional[Callable[[], None]] = None,
    conversation_id: str = "conv-fake",
) -> None:
    """Replace the one origin sender; optionally act before it returns."""
    import cli_agent_orchestrator.chatgpt_web_runner.api_drive as api_drive
    import cli_agent_orchestrator.chatgpt_web_runner.production as production
    from cli_agent_orchestrator.chatgpt_web_runner.api_drive import OriginResult

    async def _send_once(captured: Any, **kwargs: Any) -> OriginResult:
        # The real ledger writes still happen: this is the one-way reservation.
        await captured.route_holder.guard_for_python(kwargs["live_generations"])
        kwargs["intent_log"].reserve_mint()
        await captured.route_holder.mark_python_invoked(kwargs["live_generations"])
        kwargs["intent_log"].record_python_post_invoked()
        if before_return is not None:
            before_return()
        return OriginResult(
            status_code=200,
            content_type="text/event-stream",
            bytes_drained=48,
            relay_status="skipped",
            projection=(),
            conversation_id=conversation_id,
            assistant_message_id="asst-fake-1",
        )

    monkeypatch.setattr(production, "send_once", _send_once, raising=False)
    monkeypatch.setattr(api_drive, "send_once", _send_once, raising=False)


def install_get(monkeypatch: Any, *, branch_digest: str = "sha256:branch-fake") -> None:
    import cli_agent_orchestrator.chatgpt_web_runner.detached_transport as detached
    import cli_agent_orchestrator.chatgpt_web_runner.production as production
    from cli_agent_orchestrator.chatgpt_web_runner.poll_gate import (
        REQUIRED_MODEL_SLUG,
        REQUIRED_THINKING_EFFORT,
        AcceptedAnswer,
    )

    async def _poll(**kwargs: Any) -> tuple[AcceptedAnswer, str]:
        return (
            AcceptedAnswer(
                text="Finding 1: Cite: D3 line 4. OLD: `a`. REPLACE: `b`. Evidence: note.",
                model_slug=REQUIRED_MODEL_SLUG,
                thinking_effort=REQUIRED_THINKING_EFFORT,
                conversation_id=str(kwargs["conversation_id"]),
                assistant_node_id="asst-fake-1",
            ),
            branch_digest,
        )

    monkeypatch.setattr(production, "poll_authoritative_get", _poll, raising=False)
    monkeypatch.setattr(detached, "poll_authoritative_get", _poll, raising=False)
