"""Real-Chromium harness for the F862 Amendment D AC-25 disposition oracle.

The offline fake origin (``chatgpt_web_fake_origin``) supplies the independent
receipt ledger. This module supplies the other half the readiness review found
missing: a *real* Playwright page that issues the routed conversation POST
through the production route handler (``capture_held_route``), a real
long-lived holder task, real ``page.on(...)`` request events wired into
``HeldRoute.observe``, and a real teardown surface (navigate / close page /
close context / disconnect / kill Chromium / kill driver / cancel the holder).

Nothing here touches chatgpt.com or any logged-in profile: Chromium is launched
with a throwaway user-data-dir against a loopback HTTPS origin.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from cli_agent_orchestrator.chatgpt_web_runner.in_page_transport import (
    CapturedSend,
    HeldRoute,
    LiveGenerations,
    RouteCustodyError,
    RouteDisposition,
    capture_held_route,
)

CONVERSATION_GLOB = "**/backend-api/f/conversation"

#: Unknown Chromium switches are ignored by the browser, so this marker gives
#: the harness a reliable way to find the real browser PID for the kill arm.
HARNESS_SWITCH = "--f862-harness-id"


_UNAVAILABLE_REASON: Optional[str] = None


def chromium_available() -> bool:
    """True when a launchable Playwright Chromium build exists for this interpreter.

    A pure directory-layout guess is a silent false-negative machine: it turns
    "the browser moved" into "13 tests skipped", which is exactly how a
    load-bearing oracle stops being run without anyone noticing. (It did:
    Playwright 151 ships the binary under ``chrome-linux64/``.) So the probe
    falls back to Playwright's own registry, ``chromium.executable_path``, and
    ``chromium_unavailable_reason()`` carries the diagnosis into the skip
    message so a skipped run is still legible.
    """
    global _UNAVAILABLE_REASON
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - import guard
        _UNAVAILABLE_REASON = f"playwright python package not importable: {exc!r}"
        return False
    # Fast path: a plain directory probe, so the common (installed) case never
    # spawns the node driver at collection time. Playwright has shipped the
    # binary under chrome-linux/, chrome-linux64/ and chrome-headless-shell-*/
    # across versions, so probe by FILE NAME and let the registry arbitrate
    # when the probe comes up empty.
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(
        Path.home() / ".cache" / "ms-playwright"
    )
    base = Path(root)
    if base.is_dir():
        for binary in ("chrome", "chrome-headless-shell", "headless_shell"):
            for found in base.glob(f"chromium*/*/{binary}"):
                if found.is_file():
                    return True
    # Slow path: ask the registry, and keep its diagnosis for the skip reason.
    try:
        with sync_playwright() as p:
            executable = Path(p.chromium.executable_path)
    except Exception as exc:
        _UNAVAILABLE_REASON = (
            f"playwright driver/registry unusable: {exc!r}; "
            f"PLAYWRIGHT_BROWSERS_PATH={os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '<unset>')}"
        )
        return False
    if not executable.exists():
        _UNAVAILABLE_REASON = (
            f"chromium build missing at {executable} " f"(run `uv run playwright install chromium`)"
        )
        return False
    return True


def chromium_unavailable_reason() -> str:
    """Why :func:`chromium_available` said no (empty when it said yes)."""
    return _UNAVAILABLE_REASON or ""


def _pids_with(token: str) -> list[int]:
    """Every live PID whose cmdline contains ``token`` (Linux /proc scan)."""
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if token.encode() in cmdline:
            found.append(int(entry.name))
    return found


def _descendant_driver_pid() -> Optional[int]:
    """The Playwright node driver running beneath THIS python process, if any.

    Walks our own descendants rather than scanning the whole machine, so a
    second harness (or another lane on the same box) is never killed.
    """
    try:
        import psutil
    except Exception:  # pragma: no cover - psutil is a runtime dependency
        return None
    try:
        me = psutil.Process()
        for child in me.children(recursive=True):
            try:
                cmdline = " ".join(child.cmdline())
            except Exception:
                continue
            if "playwright" in cmdline and ("driver" in cmdline or "run-driver" in cmdline):
                return int(child.pid)
    except Exception:
        return None
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class HeldRouteSession:
    """One intercepted conversation POST and everything observed about it."""

    attempt_id: str
    mint_id: str
    profile_epoch: str
    generations: LiveGenerations
    #: Opened by the production handler once the route is captured and held.
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    #: Set when the holder writes a terminal disposition.
    terminal: asyncio.Event = field(default_factory=asyncio.Event)
    #: Set when the holder task returns (normally or by cancellation).
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    captured: Optional[CapturedSend] = None
    holder: Optional[HeldRoute] = None
    holder_task: "Optional[asyncio.Task[Any]]" = None
    #: Every public Playwright request event seen for the held request, in order.
    events: list[str] = field(default_factory=list)
    #: Set when the handler refused a non-conversation route.
    refused: Optional[str] = None
    #: Scratch space an action can use to publish in-handler observations
    #: (e.g. the guard's refusal, which must be taken while the task is live).
    notes: dict = field(default_factory=dict)
    #: Opened by the test once its teardown injection has been applied.
    teardown_injected: asyncio.Event = field(default_factory=asyncio.Event)
    pending: "list[asyncio.Task[Any]]" = field(default_factory=list)

    @property
    def disposition(self) -> Optional[RouteDisposition]:
        return None if self.holder is None else self.holder.disposition

    async def settle(self, timeout: float = 2.0) -> None:
        """Let queued event observations finish before an assertion reads state."""
        await asyncio.sleep(0)
        if self.pending:
            done_soon = list(self.pending)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.gather(*done_soon, return_exceptions=True), timeout)
        await asyncio.sleep(0)

    async def wait_terminal(self, timeout: float = 10.0) -> RouteDisposition:
        await asyncio.wait_for(self.terminal.wait(), timeout)
        assert self.holder is not None
        return self.holder.disposition


HandlerAction = Callable[[HeldRouteSession], Awaitable[None]]


class RealBrowserHarness:
    """Own one Chromium, one context, one page and one held-route session."""

    def __init__(
        self,
        *,
        origin: Any,
        headless: bool = True,
    ) -> None:
        self.origin = origin
        self.headless = headless
        self.token = f"f862-{uuid.uuid4().hex[:12]}"
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self.generations: Optional[LiveGenerations] = None
        self.session: Optional[HeldRouteSession] = None
        self._release = asyncio.Event()
        self._chromium_pids: list[int] = []
        self._driver_pid: Optional[int] = None

    # --- lifecycle --------------------------------------------------------
    async def __aenter__(self) -> "RealBrowserHarness":
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self._driver_pid = self._discover_driver_pid()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=[
                f"{HARNESS_SWITCH}={self.token}",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        self._chromium_pids = _pids_with(self.token)
        self.context = await self.browser.new_context(ignore_https_errors=True)
        self.page = await self.context.new_page()
        self.generations = LiveGenerations(self.page, self.context)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        for closer in (self.context, self.browser):
            if closer is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(closer.close(), 5)
        if self.playwright is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.playwright.stop(), 5)
        # Orphans from the kill/driver-death arms.
        for pid in _pids_with(self.token):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)

    def _discover_driver_pid(self) -> Optional[int]:
        """PID of the node driver process this Playwright instance just spawned.

        Playwright's private transport attribute has moved between releases
        (``_connection._transport._proc`` is absent on 1.5x), and an absent
        driver PID silently disables the driver-death arm. So the private path
        is only a hint: the authoritative answer is our own process tree, where
        the driver is the descendant running ``.../playwright/driver/node``.
        """
        connection = getattr(self.playwright, "_connection", None)
        transport = getattr(connection, "_transport", None)
        proc = getattr(transport, "_proc", None)
        pid = getattr(proc, "pid", None)
        if isinstance(pid, int) and _pid_alive(pid):
            return int(pid)
        return _descendant_driver_pid()

    # --- interception -----------------------------------------------------
    async def arm_route(
        self,
        *,
        action: Optional[HandlerAction] = None,
        attempt_id: str = "attempt-real",
        mint_id: str = "mint-real",
        profile_epoch: str = "epoch-real",
    ) -> HeldRouteSession:
        """Install the production capture handler before any composer trigger."""
        assert self.generations is not None
        session = HeldRouteSession(
            attempt_id=attempt_id,
            mint_id=mint_id,
            profile_epoch=profile_epoch,
            generations=self.generations,
        )
        self.session = session

        async def handler(route: Any) -> None:
            session.holder_task = asyncio.current_task()
            try:
                captured = await capture_held_route(
                    route,
                    attempt_id=attempt_id,
                    mint_id=mint_id,
                    profile_epoch=profile_epoch,
                    generations=self.generations.current,  # type: ignore[union-attr]
                )
            except RouteCustodyError as exc:
                session.refused = str(exc)
                session.finished.set()
                return
            session.captured = captured
            session.holder = captured.route_holder
            self._wire_request_events(session, captured.route_holder.request)
            session.entered.set()
            try:
                if action is None:
                    await self._release.wait()
                else:
                    await action(session)
            finally:
                session.finished.set()

        await self.page.route(CONVERSATION_GLOB, handler)
        return session

    def _wire_request_events(self, session: HeldRouteSession, request: Any) -> None:
        """Feed public page request events for THIS request into the holder."""

        def make(event_name: str) -> Callable[[Any], None]:
            def _on(payload: Any) -> None:
                observed = getattr(payload, "request", payload)
                if observed is not request and getattr(observed, "url", None) != getattr(
                    request, "url", None
                ):
                    return
                session.events.append(event_name)
                session.pending.append(asyncio.ensure_future(self._observe(session, event_name)))

            return _on

        for name in ("requestfailed", "requestfinished", "response"):
            self.page.on(name, make(name))

    async def _observe(self, session: HeldRouteSession, event_name: str) -> None:
        holder = session.holder
        if holder is None:
            return
        disposition = await holder.observe(event_name)
        if holder.terminal_written_at is not None:
            session.terminal.set()
        _ = disposition

    def release_holder(self) -> None:
        """Let a default-action holder task return."""
        self._release.set()

    # --- page driving -----------------------------------------------------
    async def open_page(self) -> None:
        await self.page.goto(self.origin.origin, wait_until="load")

    async def trigger_conversation_post(self) -> None:
        """Dispatch the page's own fetch without awaiting its promise."""
        await self.page.evaluate("void window.issueConversation()")

    async def result_text(self) -> str:
        return str(await self.page.inner_text("#result"))

    # --- teardown injections ---------------------------------------------
    async def inject_navigation(self) -> None:
        with contextlib.suppress(Exception):
            await self.page.goto("about:blank", wait_until="commit", timeout=5000)

    async def inject_page_close(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.page.close(), 5)

    async def inject_context_close(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.context.close(), 5)

    async def inject_browser_disconnect(self) -> None:
        """Playwright transport disconnect: the browser connection goes away."""
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.browser.close(), 5)

    async def inject_chromium_kill(self) -> None:
        """SIGKILL every real Chromium process of this harness."""
        pids = _pids_with(self.token) or self._chromium_pids
        assert pids, "no Chromium process found for this harness"
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        for _ in range(100):
            if not any(_pid_alive(pid) for pid in pids):
                return
            await asyncio.sleep(0.05)

    async def inject_driver_death(self) -> None:
        """Kill the Playwright node driver, leaving Chromium alive."""
        assert self._driver_pid is not None, "driver pid not discoverable"
        with contextlib.suppress(OSError):
            os.kill(self._driver_pid, signal.SIGKILL)
        for _ in range(100):
            if not _pid_alive(self._driver_pid):
                return
            await asyncio.sleep(0.05)

    async def inject_worker_cancellation(self, session: HeldRouteSession) -> None:
        """Cancel the holder task exactly as a cancelled worker would."""
        assert session.holder_task is not None
        session.holder_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(session.holder_task), 5)
        await asyncio.sleep(0)

    def chromium_alive(self) -> bool:
        return any(_pid_alive(pid) for pid in (_pids_with(self.token) or self._chromium_pids))
