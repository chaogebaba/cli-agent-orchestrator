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

import re
from dataclasses import dataclass
from typing import Any, Optional

from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
)

# --- DOM locators (humanize-safe CSS / testid), findings §7 --------------------
SEL_COMPOSER = "div.ProseMirror[contenteditable='true']"
SEL_USER_TURN = "div[data-message-author-role='user']"
SEL_ASSISTANT_TURN = "div[data-message-author-role='assistant']"
SEL_FILE_INPUT = "input#upload-files"
SEL_PLUS_BTN = "#composer-plus-btn"


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


def build_conversation_fetch_script(conversation_id: str) -> str:
    """Return the in-page JS that reads the conversation GET (D3).

    The bearer is acquired from ``/api/auth/session`` and used ONLY inside this
    in-page fetch; it is never returned to the host. The returned object carries
    HTTP status, node count, and the current node's allowlisted fields — NEVER a
    header, cookie or bearer value (AC-11). The conversation id is validated by
    the caller against the owned id BEFORE this runs (read containment, AC-11b).
    """
    # The id is a validated uuid by the time this is built; still, embed it as a
    # JSON string to avoid any template injection.
    import json as _json

    cid = _json.dumps(conversation_id)
    return (
        "async () => {\n"
        "  let bearer = '';\n"
        "  try {\n"
        "    const s = await fetch('/api/auth/session', {credentials:'include'});\n"
        "    const sj = await s.json();\n"
        "    bearer = (sj && sj.accessToken) || '';\n"
        "  } catch (e) {}\n"
        "  const headers = {};\n"
        "  if (bearer) headers['authorization'] = 'Bearer ' + bearer;\n"
        f"  const r = await fetch('/backend-api/conversation/' + {cid}, "
        "{credentials:'include', headers});\n"
        "  const out = {httpStatus: r.status, ok: r.ok, body: null};\n"
        "  if (!r.ok) return out;\n"
        "  try { out.body = JSON.parse(await r.text()); } catch (e) {}\n"
        "  return out;\n"
        "}"
    )


def readback_ok(source_text: str, composer_non_space_len: int, *, ratio: float = 0.6) -> bool:
    """Composer readback pass condition (ask.ts:423): the composer holds at least
    ``ratio`` of the prompt's non-space char count (ProseMirror reflows
    whitespace, so an exact match is wrong)."""
    want_min = int(len(re.sub(r"\s", "", source_text)) * ratio)
    return composer_non_space_len >= want_min


# --- Browser-driving async methods (Playwright used only here) -----------------


class Transport:
    """Drives one page: type, attach, submit, observe, read. Owner-scoped.

    The constructor takes an already-open Playwright ``page``; the runtime module
    owns launch/teardown. All completion decisions are delegated to
    ``poll_gate.evaluate_gate`` — this class only FETCHES, never JUDGES (D6).
    """

    def __init__(self, page: Any, owned_conversation_id: Optional[str] = None) -> None:
        self.page = page
        self.owned_conversation_id = owned_conversation_id
        self.saw_send = False

    def arm_send_observer(self) -> None:
        """Observe the SSE send response for early conversation-id discovery
        (ask.ts:633-645). We never consume the SSE body (would block); the URL
        and the conversation GET are the load-bearing signals (D6)."""
        from cli_agent_orchestrator.chatgpt_web_runner.submit_ids import is_send_response_url

        def _on_response(resp: Any) -> None:
            try:
                if is_send_response_url(resp.url):
                    self.saw_send = True
            except Exception:
                pass

        self.page.on("response", _on_response)

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

    async def read_conversation(self, conversation_id: str) -> dict[str, Any]:
        """Read the conversation GET via the containment-checked in-page fetch."""
        from cli_agent_orchestrator.chatgpt_web_runner.snapshot_upload import (
            enforce_read_allowed,
        )

        url = f"https://chatgpt.com/backend-api/conversation/{conversation_id}"
        enforce_read_allowed(url, conversation_id)
        script = build_conversation_fetch_script(conversation_id)
        result = await self.page.evaluate(script)
        return result if isinstance(result, dict) else {"httpStatus": 0, "ok": False, "body": None}
