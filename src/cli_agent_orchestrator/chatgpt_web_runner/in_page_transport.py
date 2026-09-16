"""In-page transport: DOM submit, SSE observe, conversation-GET, upload (D3/D6).

The browser-facing half of the runner. Playwright types are used only inside the
async methods; the module imports without a browser. The failure-surface
classification helpers (auth / captcha / bot_flagged / quota / access_denied,
D4/AC-14) and the in-page fetch SCRIPT builder are pure and unit-testable.

Measured seams reimplemented from the bun spike (cited per the blueprint's
"Measured seams" list):
  - composer ``div.ProseMirror[contenteditable='true']`` (ask.ts:191)
  - text entry via ``keyboard.insert_text`` — NEVER ``fill`` (ask.ts:429; fill
    stalls ~97s on this ProseMirror), with a >=60% non-space readback (ask.ts:423)
  - submit by ``Enter`` on the composer (ask.ts:686)
  - bearer from ``GET /api/auth/session`` used ONLY inside the in-page fetch
    (ask.ts:506-513) — never returned to the host
  - conversation read + node walk (ask.ts:501-545)
  - send observation on ``POST /backend-api/f/conversation`` excluding ``/prepare``
    (ask.ts:633-645)
  - attach via ``input#upload-files`` (ask.ts:207,328)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Optional, cast

from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
)

logger = logging.getLogger(__name__)

# --- DOM locators (humanize-safe CSS / testid), findings §7 --------------------
SEL_COMPOSER = "div.ProseMirror[contenteditable='true']"
SEL_USER_TURN = "div[data-message-author-role='user']"
SEL_ASSISTANT_TURN = "div[data-message-author-role='assistant']"


class RouteDisposition(str, Enum):
    """Holder-owned truth about the intercepted browser request (Amendment D)."""

    HELD = "held"
    FULFILLING = "fulfilling"
    FULFILLED = "fulfilled"
    ABORTED = "aborted"
    LOST = "lost"
    RELEASED_TO_ORIGIN = "released_to_origin"


_TERMINAL_ROUTE_DISPOSITIONS = {
    RouteDisposition.FULFILLED,
    RouteDisposition.ABORTED,
    RouteDisposition.LOST,
    RouteDisposition.RELEASED_TO_ORIGIN,
}


class RouteCustodyError(RuntimeError):
    """An operation would violate the one-owner held-route interlock."""


@dataclass(frozen=True)
class RouteGenerations:
    page: int
    context: int
    cdp_session: int


@dataclass(frozen=True)
class CapturedSend:
    """The single in-memory packet minted by the real composer.

    Header values and body bytes are intentionally excluded from ``repr`` and
    never have a serializer. Only their names/digest may enter durable state.
    """

    route_holder: "HeldRoute"
    method: str
    url: str
    ordered_headers: tuple[tuple[str, str], ...] = dataclass_field(repr=False)
    raw_body: bytes = dataclass_field(repr=False)
    captured_monotonic: float
    profile_epoch: str
    attempt_id: str
    mint_id: str

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.raw_body).hexdigest()

    @property
    def header_names(self) -> tuple[str, ...]:
        return tuple(name.lower() for name, _value in self.ordered_headers)


class HeldRoute:
    """One live Playwright ``Route`` with a first-terminal-wins disposition.

    Playwright has no public "still held" predicate. This object is therefore
    the sole authority. Every action/event proposal is serialized by one lock;
    terminal state can be written exactly once, and no private Playwright field
    is inspected.
    """

    def __init__(
        self,
        route: Any,
        request: Any,
        *,
        attempt_id: str,
        generations: RouteGenerations,
        owner_task: Optional["asyncio.Task[Any]"] = None,
    ) -> None:
        self.route = route
        self.request = request
        self.attempt_id = attempt_id
        self.generations = generations
        self._owner_task = owner_task or asyncio.current_task()
        self._lock = asyncio.Lock()
        self._disposition = RouteDisposition.HELD
        self._python_invoked = False
        self._terminal_written_at: Optional[float] = None
        self._owner_death_settled = False
        # D1 (r3 table): a holder task that stops existing without calling
        # abort()/fulfil()/on_teardown() leaves a route nobody can prove was
        # un-sent, so its disposition is `lost` — never `aborted`. A `lost`
        # route terminates the attempt as ACK_UNKNOWN; ABANDONED_PRE_INVOKE is
        # legal ONLY from a holder-owned abort that succeeded while the route
        # was still HELD, which is why record_abandoned_pre_invoke() refuses
        # any other disposition (send_intent.py) and why only `aborted`
        # authorises a fresh same-turn mint.
        #
        # Without this callback the disposition would stay HELD forever: the
        # send guard still refuses (it checks the live task), but the attempt
        # could never record a terminal and forbid_while_held() would lock out
        # its own cleanup. The holder writing its own terminal as it dies keeps
        # the one-writer rule intact, and _set_terminal is first-terminal-wins,
        # so a real terminal already written by fulfil/abort/observe wins.
        #
        # B3 composition constraint (B1 review, probe 4): this fires on ANY
        # owner-task completion, including a NORMAL return. See
        # capture_held_route's docstring — the owner must stay alive for the
        # whole custody window, or custody must be handed over explicitly via
        # owner_task=.
        if self._owner_task is not None:
            self._owner_task.add_done_callback(self._on_owner_done)

    def _on_owner_done(self, _task: "asyncio.Task[Any]") -> None:
        """Write the fail-closed terminal when the holder task stops existing."""
        self._owner_death_settled = True
        if self._disposition not in _TERMINAL_ROUTE_DISPOSITIONS:
            # Conservative by construction: a hold whose owner died mid-flight
            # is never provably un-sent, so it is `lost`, never `aborted`.
            self._set_terminal(RouteDisposition.LOST)

    @property
    def owner_death_settled(self) -> bool:
        """True once the holder task has ended and its terminal is written."""
        return self._owner_death_settled

    @property
    def disposition(self) -> RouteDisposition:
        return self._disposition

    @property
    def terminal_written_at(self) -> Optional[float]:
        return self._terminal_written_at

    @property
    def python_invoked(self) -> bool:
        return self._python_invoked

    def _owner_alive(self) -> bool:
        return bool(
            self._owner_task is not None
            and not self._owner_task.done()
            and not self._owner_task.cancelled()
        )

    async def guard_for_python(self, live_generations: RouteGenerations) -> None:
        """Refuse unless the same live holder still owns a genuinely held route."""
        async with self._lock:
            if not self._owner_alive():
                raise RouteCustodyError("route holder task is not live")
            if live_generations != self.generations:
                raise RouteCustodyError("page/context/CDP generation changed")
            if self._disposition is not RouteDisposition.HELD:
                raise RouteCustodyError(
                    f"route is {self._disposition.value}, not held; Python POST refused"
                )

    async def mark_python_invoked(self, live_generations: RouteGenerations) -> None:
        await self.guard_for_python(live_generations)
        async with self._lock:
            if self._python_invoked:
                raise RouteCustodyError("Python POST already invoked for this held mint")
            self._python_invoked = True

    async def observe(self, event: str) -> RouteDisposition:
        """Apply a public Playwright request event to holder state."""
        if event not in {"requestfailed", "requestfinished", "response"}:
            raise ValueError(f"unsupported route event {event!r}")
        async with self._lock:
            if self._disposition in _TERMINAL_ROUTE_DISPOSITIONS:
                return self._disposition
            if event == "requestfailed":
                return self._set_terminal(RouteDisposition.LOST)
            if self._disposition is RouteDisposition.FULFILLING:
                return self._set_terminal(RouteDisposition.FULFILLED)
            # A response/finish while still HELD proves the browser copy escaped.
            return self._set_terminal(RouteDisposition.RELEASED_TO_ORIGIN)

    async def on_teardown(self) -> RouteDisposition:
        async with self._lock:
            if self._disposition in _TERMINAL_ROUTE_DISPOSITIONS:
                return self._disposition
            return self._set_terminal(RouteDisposition.LOST)

    async def abort(self) -> RouteDisposition:
        """Abort is resend-safe only when this call itself succeeds from HELD."""
        async with self._lock:
            if self._disposition in _TERMINAL_ROUTE_DISPOSITIONS:
                return self._disposition
            if self._disposition is not RouteDisposition.HELD:
                raise RouteCustodyError(f"cannot abort from {self._disposition.value}")
            try:
                await self.route.abort()
            except Exception:
                return self._set_terminal(RouteDisposition.LOST)
            return self._set_terminal(RouteDisposition.ABORTED)

    async def fulfil(self, *, body: bytes) -> RouteDisposition:
        """Complete the browser copy locally exactly once."""
        async with self._lock:
            if self._disposition is not RouteDisposition.HELD:
                raise RouteCustodyError(f"cannot fulfil from {self._disposition.value}")
            self._disposition = RouteDisposition.FULFILLING
        try:
            await self.route.fulfill(
                status=200,
                headers={"content-type": "text/event-stream; charset=utf-8"},
                body=body,
            )
        except Exception:
            async with self._lock:
                if self._disposition is RouteDisposition.FULFILLING:
                    return self._set_terminal(RouteDisposition.LOST)
                return self._disposition
        async with self._lock:
            if self._disposition is RouteDisposition.FULFILLING:
                return self._set_terminal(RouteDisposition.FULFILLED)
            return self._disposition

    async def forbid_while_held(self, operation: str) -> None:
        """Guard navigation/close/unroute/lease handoff/cancellation-with-release."""
        async with self._lock:
            if self._disposition not in _TERMINAL_ROUTE_DISPOSITIONS:
                raise RouteCustodyError(
                    f"{operation} forbidden while route disposition is {self._disposition.value}"
                )

    def _set_terminal(self, proposal: RouteDisposition) -> RouteDisposition:
        if self._disposition in _TERMINAL_ROUTE_DISPOSITIONS:
            return self._disposition
        self._disposition = proposal
        self._terminal_written_at = time.monotonic()
        return proposal


class LiveGenerations:
    """Derive the three D1 generations from public Playwright lifecycle events.

    ``HeldRoute.guard_for_python`` compares the generations recorded at
    ``REQUEST_HELD`` against the live ones; something has to produce the live
    triple from real objects. This tracker is that seam: it subscribes to the
    public ``close``/``crash``/``disconnected`` events and bumps the matching
    counter, so a page/context/browser that died between the hold and the
    socket call is observable without reading any private Playwright state.

    It is deliberately monotonic and never decrements: a bumped generation can
    only make the guard refuse, never admit.
    """

    def __init__(
        self,
        page: Any,
        context: Any = None,
        cdp_session: Any = None,
    ) -> None:
        self._page_gen = 1
        self._context_gen = 1
        self._cdp_gen = 1
        self._page = page
        self._context = context
        self._cdp_session = cdp_session
        if page is not None:
            page.on("close", lambda *_a: self.bump_page())
            page.on("crash", lambda *_a: self.bump_page())
        if context is not None:
            context.on("close", lambda *_a: self.bump_context())
        if cdp_session is not None:
            # A detached CDP session invalidates every command issued on it.
            on = getattr(cdp_session, "on", None)
            if callable(on):
                on("detached", lambda *_a: self.bump_cdp_session())

    def bump_page(self) -> None:
        self._page_gen += 1

    def bump_context(self) -> None:
        self._context_gen += 1
        # A dead context takes its pages with it.
        self._page_gen += 1

    def bump_cdp_session(self) -> None:
        self._cdp_gen += 1

    @property
    def current(self) -> RouteGenerations:
        return RouteGenerations(
            page=self._page_gen,
            context=self._context_gen,
            cdp_session=self._cdp_gen,
        )


def is_conversation_post(method: str, url: str) -> bool:
    """Match only the real conversation POST, never a ``/prepare`` request."""
    return (
        method.upper() == "POST" and "/backend-api/f/conversation" in url and "/prepare" not in url
    )


async def capture_held_route(
    route: Any,
    *,
    attempt_id: str,
    mint_id: str,
    profile_epoch: str,
    generations: RouteGenerations,
) -> CapturedSend:
    """Capture a matching Playwright route and leave it unresolved.

    This callback is intentionally tiny: it performs no ``continue_`` or
    network action. The caller persists ``REQUEST_HELD`` using the returned
    digest/name metadata before the one-shot sender is admitted.

    **Holder-task lifetime contract (D1; B1 review probe 4).** The returned
    :class:`HeldRoute` binds its owner to ``asyncio.current_task()`` — i.e. to
    the Playwright route-handler task that called this function. That task MUST
    stay alive for the whole custody window, from capture through
    ``fulfil()``/``abort()``. ``HeldRoute`` writes the fail-closed ``lost``
    terminal when its owner task completes **for any reason, including a normal
    return**, so a handler that captures the route and then returns classifies
    its own live route ``lost``, refuses the Python POST, and unlocks
    ``forbid_while_held`` on a request Playwright is still holding paused.

    Compositions therefore either park the handler task (await an event that
    the custody window's end sets — what ``RealBrowserHarness`` and
    :mod:`~cli_agent_orchestrator.chatgpt_web_runner.production` both do), or
    hand custody over explicitly by constructing the :class:`HeldRoute` with
    ``owner_task=`` naming the task that will outlive the window. There is no
    third option: an implicitly-owned route whose handler returns is always
    ``lost``.
    """
    request = route.request
    method = str(getattr(request, "method", "POST"))
    url = str(getattr(request, "url", ""))
    if not is_conversation_post(method, url):
        # Non-conversation traffic must retain its normal page behaviour.
        await route.continue_()
        raise RouteCustodyError("route did not match the conversation POST")
    body = getattr(request, "post_data_buffer", None)
    if callable(body):
        body = body()
    if hasattr(body, "__await__"):
        body = await cast(Awaitable[Any], body)
    if body is None:
        post_data = getattr(request, "post_data", None)
        body = post_data.encode("utf-8") if isinstance(post_data, str) else post_data
    if not isinstance(body, bytes):
        raise RouteCustodyError("conversation POST body unavailable; held proof failed")
    raw_headers = getattr(request, "headers", {}) or {}
    ordered = tuple((str(k), str(v)) for k, v in raw_headers.items())
    holder = HeldRoute(
        route,
        request,
        attempt_id=attempt_id,
        generations=generations,
    )
    return CapturedSend(
        route_holder=holder,
        method=method,
        url=url,
        ordered_headers=ordered,
        raw_body=body,
        captured_monotonic=time.monotonic(),
        profile_epoch=profile_epoch,
        attempt_id=attempt_id,
        mint_id=mint_id,
    )


def synthetic_v1_stream(
    *,
    posted_user_message: dict[str, Any],
    conversation_id: str,
    assistant_id: str,
    final_text: str,
) -> bytes:
    """Build D4's seven semantic frames from the exact Python-posted object."""
    root = {
        "v": {
            "message": {
                "id": assistant_id,
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": [""]},
                "status": "in_progress",
                "end_turn": False,
            },
            "conversation_id": conversation_id,
        }
    }
    frames: list[tuple[Optional[str], Any]] = [
        ("delta_encoding", "v1"),
        (None, root),
        (
            "input_message",
            {"message": posted_user_message, "conversation_id": conversation_id},
        ),
        (None, {"p": "/message/content/parts/0", "o": "append", "v": final_text}),
        (
            None,
            [
                {"p": "/message/status", "o": "replace", "v": "finished_successfully"},
                {"p": "/message/end_turn", "o": "replace", "v": True},
            ],
        ),
        ("message_stream_complete", {"conversation_id": conversation_id}),
    ]
    chunks: list[bytes] = []
    for event, payload in frames:
        if event is not None:
            chunks.append(f"event: {event}\n".encode())
        chunks.append(("data: " + json.dumps(payload, separators=(",", ":")) + "\n\n").encode())
    chunks.append(b"data: [DONE]\n\n")
    return b"".join(chunks)


@dataclass(frozen=True)
class FailureSurface:
    """A classified chrome/driver-state failure (D4). NEVER classified from
    assistant TEXT — only from chrome/driver state (D4 Do-NOT)."""

    code: RunnerErrorCode
    subtype: Optional[str] = None
    reset_hint: Optional[str] = None


def classify_failure_surface(
    *,
    http_status: Optional[int] = None,
    challenge_document: bool = False,
    unusual_activity_marker: bool = False,
    auth_wall_marker: bool = False,
    captcha_frame: bool = False,
    quota_banner: bool = False,
    quota_reset_hint: Optional[str] = None,
) -> Optional[FailureSurface]:
    """Classify a failure from CHROME/DRIVER STATE (D4/AC-14).

    Ordering keeps an auth expiry from being reported as a bot flag (the AC-14
    three-way discrimination). ``bot_flagged`` requires an EXPLICIT unusual-
    activity/automation denial or a known challenge document PLUS a recorded
    status — never a generic 403 (D4 Do-NOT). An unclassifiable 403 becomes
    ``access_denied`` and fails closed, human-gated.
    """
    if auth_wall_marker or http_status == 401:
        return FailureSurface(RunnerErrorCode.AUTH_WALL)
    if captcha_frame:
        return FailureSurface(RunnerErrorCode.CAPTCHA, subtype="captcha")
    if challenge_document or unusual_activity_marker:
        # A CONFIRMED bot flag: explicit automation denial or challenge doc.
        return FailureSurface(RunnerErrorCode.BOT_FLAGGED, subtype="bot_flagged")
    if quota_banner:
        return FailureSurface(RunnerErrorCode.QUOTA, reset_hint=quota_reset_hint)
    if http_status == 403:
        # Unknown 403 with no explicit challenge marker: fail closed.
        return FailureSurface(RunnerErrorCode.ACCESS_DENIED, subtype="unknown")
    return None


def readback_ok(source_text: str, composer_non_space_len: int, *, ratio: float = 0.6) -> bool:
    """Composer readback pass condition (ask.ts:423): the composer holds at least
    ``ratio`` of the prompt's non-space char count (ProseMirror reflows
    whitespace, so an exact match is wrong)."""
    want_min = int(len(re.sub(r"\s", "", source_text)) * ratio)
    return composer_non_space_len >= want_min


# --- Browser-driving async methods (Playwright used only here) -----------------


def _newest_user_msg_id(conv: dict[str, Any]) -> str:
    """Return the message id of the newest USER node in a conversation body.

    A fresh single-turn conversation has exactly one user turn, so the newest
    user node is the just-submitted one — used to lazily resolve the ancestry
    anchor when the pre-poll resolution came up empty (F862 r2)."""
    mapping_obj = conv.get("mapping")
    mapping = mapping_obj if isinstance(mapping_obj, dict) else {}
    best = ""
    best_ct = -1.0
    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        msg = node.get("message")
        if not isinstance(msg, dict):
            continue
        author = msg.get("author")
        if isinstance(author, dict) and author.get("role") == "user":
            ct = float(msg.get("create_time") or 0.0)
            if ct >= best_ct:
                best_ct = ct
                best = str(msg.get("id") or "")
    return best


if TYPE_CHECKING:  # pragma: no cover - typing only
    from cli_agent_orchestrator.chatgpt_web_runner.send_intent import SendIntentLog


class Transport:
    """Drives one page: type, attach, submit, observe, read. Owner-scoped.

    The constructor takes an already-open Playwright ``page``; the runtime module
    owns launch/teardown. All completion decisions are delegated to
    ``poll_gate.evaluate_gate`` — this class only FETCHES, never JUDGES (D6).
    """

    def __init__(
        self,
        page: Any,
        owned_conversation_id: Optional[str] = None,
        intent_log: Optional["SendIntentLog"] = None,
    ) -> None:
        self.page = page
        self.owned_conversation_id = owned_conversation_id
        # F862 (#718) D7/D16 r6: the durable send-intent record for THIS attempt.
        # ``trigger_composer_mint`` refuses to press Enter without one, so a
        # crash between intent and dispatch is resolvable on restart. Optional
        # only so the offline DOM tests can drive the transport without a
        # filesystem; ``run_production_review`` always supplies it.
        self.intent_log: Optional["SendIntentLog"] = intent_log

    async def type_prompt(self, text: str) -> None:
        """Type via insert_text (never fill), then read back (ask.ts:429/423)."""
        composer = self.page.locator(SEL_COMPOSER).first
        await composer.wait_for(state="visible", timeout=15_000)
        await composer.click(timeout=10_000)
        await self.page.keyboard.insert_text(text)
        await self.page.wait_for_timeout(600)
        non_space = await self.page.evaluate(
            "(sel) => { const el = document.querySelector(sel);"
            " return el ? (el.textContent || '').replace(/\\s/g,'').length : -1; }",
            SEL_COMPOSER,
        )
        if not readback_ok(text, int(non_space)):
            raise RunnerError(
                RunnerErrorCode.SUBMIT_UNKNOWN,
                "composer readback under 60% — refusing to send a partial prompt",
                delivery_state=DeliveryState.NOTHING_SENT,
            )

    # ── D7/D16 send-intent custody ───────────────────────────────────────

    def _dispatch_submit_action(self) -> None:
        """Persist DISPATCHED before the submit-triggering action (D7/D16).

        A transport with no intent log is an OFFLINE DOM test double; production
        always has one (``run_production_review`` opens the attempt). Refusing
        here when one is attached but already dispatched is what makes the AC-20
        crash / disconnect / invoked-error mutants fail loudly instead of sending
        twice.
        """
        if self.intent_log is None:
            return
        self.intent_log.record_submit_dispatch()

    async def trigger_composer_mint(self) -> None:
        """Press Enter EXACTLY ONCE to mint the conversation POST (D1/D6).

        Amendment D's composer has one job: cause the browser to issue the
        conversation POST that the preinstalled route handler holds. Everything
        the old ``submit_and_confirm`` did after the keypress is deleted by D10
        — there is no delivery classification from the DOM, no URL-derived
        conversation id, no grace loop waiting for the app's own GET, and above
        all NO second Enter. The held route is the proof of interception, the
        Python POST is the send, and the detached GET is the acceptance.

        ``record_submit_dispatch`` fsyncs DISPATCHED *before* the keypress and
        raises if this attempt already dispatched, so "after that dispatch,
        never repeat the submit action" is enforced by the durable record rather
        than by a recovery budget that a loop could spend.
        """
        self._dispatch_submit_action()
        await self.page.locator(SEL_COMPOSER).first.press("Enter")

    def _observe_send(self) -> None:
        """Record a correlated send (D7/D16, AC-20 counters). No-op offline."""
        if self.intent_log is None:
            return
        from cli_agent_orchestrator.chatgpt_web_runner.send_intent import SendIntentViolation

        try:
            self.intent_log.record_send_observed()
        except SendIntentViolation as exc:  # pragma: no cover - defensive
            logger.warning("chatgpt_web send-observation not recorded: %s", exc)
