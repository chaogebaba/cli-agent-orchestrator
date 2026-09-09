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

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
)
from cli_agent_orchestrator.chatgpt_web_runner.snapshot_upload import AttachmentIdentity

logger = logging.getLogger(__name__)

# --- DOM locators (humanize-safe CSS / testid), findings §7 --------------------
SEL_COMPOSER = "div.ProseMirror[contenteditable='true']"
SEL_USER_TURN = "div[data-message-author-role='user']"
SEL_ASSISTANT_TURN = "div[data-message-author-role='assistant']"
SEL_FILE_INPUT = "input#upload-files"
SEL_PLUS_BTN = "#composer-plus-btn"


@dataclass(frozen=True)
class SubmitOutcome:
    """The classified result of the submit-confirm window (D7)."""

    delivery_state: DeliveryState
    conversation_id: Optional[str]


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
        "  const ra = r.headers.get('retry-after');\n"
        "  const out = {httpStatus: r.status, ok: r.ok, retryAfter: ra ? Number(ra) : null, body: null};\n"
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
        # The REAL backend conversation id, learned from a page-owned
        # /backend-api/conversation/<uuid> response (NOT the /c/WEB:<uuid> route
        # id — F862 r2: they differ; the route uuid 404s on the backend).
        self.backend_conversation_id: Optional[str] = None

    def arm_send_observer(self) -> None:
        """Observe page-owned responses for early conversation-id discovery (D6).

        Two signals, both page-owned and same-origin:
          - the SSE send response ``POST /backend-api/f/conversation`` (not
            ``/prepare``) sets ``saw_send`` (ask.ts:633-645);
          - any ``/backend-api/conversation/<uuid>`` the APP itself fetches
            reveals the REAL backend conversation id (the ``/c/WEB:<uuid>`` route
            id in the address bar is NOT the backend id — F862 r2 probe: the URL
            uuid 404s; the backend id is a different uuid the app GETs itself).
        """
        import re as _re

        from cli_agent_orchestrator.chatgpt_web_runner.submit_ids import is_send_response_url

        backend_get_re = _re.compile(
            r"/backend-api/conversation/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
        )

        def _on_response(resp: Any) -> None:
            try:
                url = resp.url
                if is_send_response_url(url):
                    self.saw_send = True
                m = backend_get_re.search(url)
                if m:
                    # The app's own conversation GET names the real backend id.
                    self.backend_conversation_id = m.group(1)
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

    async def attach_file(
        self, path: str, *, ready_timeout_s: float = 60.0
    ) -> "AttachmentIdentity":
        """Attach one file via ``input#upload-files`` and wait for readiness (D8/AC-9).

        Sets the file on the composer's general-purpose file input (present
        without opening the plus menu), records the pre-upload identity, and waits
        until BOTH calibrated readiness signals are present: the chip's filename
        AND a "Document" label (stable from ~2s). A decorative spinner is NOT a
        permitted signal. On timeout without both signals, raises
        ``upload_unconfirmed`` BEFORE any Enter (AC-9).
        """
        import time as _time
        from pathlib import Path as _Path

        from cli_agent_orchestrator.chatgpt_web_runner.snapshot_upload import (
            build_attachment_identity,
            readiness_reached,
        )

        data = _Path(path).read_bytes()
        filename = _Path(path).name
        identity = build_attachment_identity(data, filename)

        file_input = self.page.locator(SEL_FILE_INPUT).first
        if await file_input.count() == 0:
            # Mount the input via the plus menu without choosing a menu item.
            if await self.page.locator(SEL_PLUS_BTN).count() > 0:
                await self.page.locator(SEL_PLUS_BTN).click(timeout=8000)
                await self.page.wait_for_timeout(800)
                await self.page.keyboard.press("Escape")
        # Wait for the input to actually exist before setting files (a race here
        # left the chip empty on a fresh page — F862 r2). Then set, and if the
        # filename chip does not appear within a few seconds, set once more.
        try:
            await self.page.locator(SEL_FILE_INPUT).first.wait_for(state="attached", timeout=10_000)
        except Exception:
            pass
        await self.page.locator(SEL_FILE_INPUT).first.set_input_files(path, timeout=30_000)
        for _ in range(6):
            await self.page.wait_for_timeout(500)
            here = await self.page.evaluate(
                "(n) => ((document.body && document.body.innerText) || '').includes(n)", filename
            )
            if here:
                break
        else:
            # One re-set attempt (still nothing sent — safe).
            await self.page.locator(SEL_FILE_INPUT).first.set_input_files(path, timeout=30_000)

        deadline = _time.monotonic() + ready_timeout_s
        while _time.monotonic() < deadline:
            present = await self.page.evaluate(
                "(name) => {\n"
                "  const body = (document.body && document.body.innerText) || '';\n"
                "  const errored = /failed to upload|couldn.t upload|upload failed|"
                "unsupported file|file is too large|error processing/i.test(body);\n"
                "  const filename = body.includes(name);\n"
                "  const stem = name.replace(/\\.[^.]+$/, '');\n"
                "  const filename_stem = body.includes(stem);\n"
                "  const document_label = /\\bDocument\\b/.test(body);\n"
                "  // capture labels near any chip that mentions the stem\n"
                "  let chip = '';\n"
                "  const els = [...document.querySelectorAll('*')].filter(e => "
                "(e.textContent||'').includes(stem) && (e.textContent||'').length < 120);\n"
                "  if (els.length) chip = (els[els.length-1].textContent || '').slice(0, 100);\n"
                "  return {errored, filename, filename_stem, document_label, chip};\n"
                "}",
                filename,
            )
            if present.get("errored"):
                raise RunnerError(
                    RunnerErrorCode.UPLOAD_UNCONFIRMED,
                    "attachment upload reported an error/unsupported state",
                    delivery_state=DeliveryState.NOTHING_SENT,
                )
            signals = set()
            if present.get("filename"):
                signals.add("filename_chip")
            if present.get("document_label"):
                signals.add("document_label")
            logger.debug(
                "chatgpt_web attach-diag filename=%s stem=%s document_label=%s chip=%r",
                present.get("filename"),
                present.get("filename_stem"),
                present.get("document_label"),
                present.get("chip"),
            )
            if readiness_reached(signals):
                logger.debug("chatgpt_web attachment ready: %s", sorted(signals))
                # D8/AC-9: capture a STABLE composer-side attachment reference for
                # the identity tuple. Prefer a chip test id / dom id; fall back to
                # the chip's own trimmed text. Non-empty is required (r2 gate B3).
                import dataclasses as _dc

                ref = await self.page.evaluate(
                    "(stem) => {\n"
                    "  const chips = [...document.querySelectorAll("
                    "\"[data-testid*='attachment' i], [class*='attachment' i]\")]"
                    ".filter(e => (e.textContent||'').includes(stem));\n"
                    "  const el = chips[chips.length-1];\n"
                    "  if (!el) return '';\n"
                    "  return el.getAttribute('data-testid') || el.id || "
                    "(el.textContent||'').trim().slice(0,80);\n"
                    "}",
                    filename.rsplit(".", 1)[0],
                )
                ref_str = str(ref or "").strip()
                if not ref_str:
                    # No stable composer reference observed -> fail closed (AC-9).
                    raise RunnerError(
                        RunnerErrorCode.ATTACHMENT_IDENTITY,
                        "no composer-side attachment reference observed on the chip",
                        delivery_state=DeliveryState.NOTHING_SENT,
                    )
                return _dc.replace(identity, composer_attachment_ref=ref_str)
            await self.page.wait_for_timeout(700)
        raise RunnerError(
            RunnerErrorCode.UPLOAD_UNCONFIRMED,
            "attachment did not reach both calibrated readiness signals before Enter",
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

    async def submit_and_confirm(self, timeout_s: float = 30.0) -> "SubmitOutcome":
        """Press Enter, then run the four-way submit-confirm window (D7).

        Returns the classified DeliveryState and the resolved conversation id
        (from the URL or the observed SSE). Never resends.
        """
        import time as _time

        from cli_agent_orchestrator.chatgpt_web_runner.submit_ids import (
            SubmitObservation,
            classify_delivery,
            extract_conversation_id,
        )

        users_before = await self.page.locator(SEL_USER_TURN).count()
        await self.page.locator(SEL_COMPOSER).first.press("Enter")
        deadline = _time.monotonic() + timeout_s
        conv_id: Optional[str] = self.owned_conversation_id
        delivered = False
        last_enter = _time.monotonic()
        while _time.monotonic() < deadline and not delivered:
            await self.page.wait_for_timeout(500)
            url = self.page.url
            found = extract_conversation_id(url)
            if found:
                conv_id = found
            users_now = await self.page.locator(SEL_USER_TURN).count()
            if users_now > users_before or self.saw_send or conv_id or self.backend_conversation_id:
                delivered = True
                break
            # With an attachment, a single early Enter can be ignored while the
            # upload finalizes server-side (the composer keeps its text). Re-press
            # Enter every ~4s WHILE the composer still holds the prompt — this is
            # still the nothing-sent state (safe to retry, D7), never a resend of
            # a delivered turn.
            composer_now = (await self.page.locator(SEL_COMPOSER).first.inner_text()).strip()
            if composer_now and (_time.monotonic() - last_enter) >= 4.0:
                await self.page.locator(SEL_COMPOSER).first.press("Enter")
                last_enter = _time.monotonic()
        # The REAL backend id (learned from a page-owned conversation GET) is
        # authoritative for polling; the /c/WEB:<uuid> route id is NOT (F862 r2).
        # Give the app a short grace to issue its own conversation GET.
        if delivered and not self.backend_conversation_id:
            for _ in range(20):
                if self.backend_conversation_id:
                    break
                await self.page.wait_for_timeout(500)
        resolved = self.backend_conversation_id or conv_id
        composer_text = (await self.page.locator(SEL_COMPOSER).first.inner_text()).strip()
        logger.debug(
            "chatgpt_web submit-confirm delivered=%s conv=%s backend=%s saw_send=%s cleared=%s",
            delivered,
            bool(conv_id),
            bool(self.backend_conversation_id),
            self.saw_send,
            composer_text == "",
        )
        obs = SubmitObservation(
            enter_dispatched=True,
            new_user_turn=delivered and resolved is not None,
            send_response_seen=self.saw_send,
            conversation_id=resolved,
            composer_cleared=(composer_text == ""),
            deadline_exhausted=not delivered,
        )
        state = classify_delivery(obs)
        self.owned_conversation_id = resolved
        return SubmitOutcome(delivery_state=state, conversation_id=resolved)

    async def poll_to_gate(
        self,
        conversation_id: str,
        submitted_user_msg_id: str,
        run_id: str,
        bundle_sha: str,
        *,
        timeout_s: float = 900.0,
    ) -> Any:
        """Poll the conversation GET to the binary done-gate (D6).

        Reads no faster than one per 3 seconds (AC-11b). Returns an
        AcceptedAnswer or raises a typed RunnerError; on deadline with a partial
        it raises truncated_answer carrying a dom/get partial source.
        """
        import time as _time

        from cli_agent_orchestrator.chatgpt_web_runner.poll_gate import (
            AcceptedAnswer,
            GatePending,
            evaluate_gate,
        )

        deadline = _time.monotonic() + timeout_s
        last_partial: Optional[str] = None
        polls = 0
        # D3: read no faster than 1/3s; back off to 10s while pending; honor
        # Retry-After on a 429. Start at the 3s floor and grow toward 10s.
        base_interval = 3.0
        pending_interval = 10.0
        interval = base_interval
        resolved_uid = submitted_user_msg_id
        while _time.monotonic() < deadline:
            polls += 1
            probe = await self.read_conversation(conversation_id)
            body = probe.get("body")
            status = probe.get("httpStatus")
            self._log_gate_diag(polls, probe, resolved_uid, run_id, bundle_sha)
            if probe.get("ok") and isinstance(body, dict):
                # Lazily resolve the submitted user-turn id from the SAME body if
                # it was not resolvable before polling (a slow/429'd first GET
                # left it empty — F862 r2). A fresh conversation has exactly one
                # user turn, so the newest user node IS ours.
                if not resolved_uid:
                    resolved_uid = _newest_user_msg_id(body)
                if not resolved_uid:
                    interval = base_interval
                    await self.page.wait_for_timeout(int(interval * 1000))
                    continue
                try:
                    result = evaluate_gate(
                        body,
                        submitted_user_msg_id=resolved_uid,
                        run_id=run_id,
                        bundle_sha=bundle_sha,
                    )
                except RunnerError as exc:
                    logger.warning(
                        "chatgpt_web gate typed-error poll=%s code=%s", polls, exc.code.value
                    )
                    raise
                if isinstance(result, AcceptedAnswer):
                    logger.debug("chatgpt_web gate ACCEPTED after %s polls", polls)
                    return result
                if isinstance(result, GatePending) and result.partial_text:
                    last_partial = result.partial_text
                interval = pending_interval  # successful read, still pending (D3)
            elif status == 429:
                ra = probe.get("retryAfter")
                interval = max(pending_interval, float(ra) if isinstance(ra, (int, float)) else 0.0)
            else:
                interval = pending_interval  # transient 400/5xx — do not hammer
            await self.page.wait_for_timeout(int(interval * 1000))
        err = RunnerError(
            RunnerErrorCode.TRUNCATED_ANSWER,
            "poll deadline exhausted before the gate closed",
            delivery_state=DeliveryState.DELIVERED,
        )
        if last_partial:
            from cli_agent_orchestrator.chatgpt_web_runner.output import PartialSource

            err.partial_source = PartialSource.CONVERSATION_GET  # type: ignore[attr-defined]
        raise err

    def _log_gate_diag(
        self,
        polls: int,
        probe: dict[str, Any],
        submitted_user_msg_id: str,
        run_id: str,
        bundle_sha: str,
    ) -> None:
        """Log a compact, NON-SECRET summary of the current node for gate debugging."""
        body = probe.get("body")
        if not isinstance(body, dict):
            logger.debug(
                "chatgpt_web gate-diag poll=%s httpStatus=%s ok=%s no-body",
                polls,
                probe.get("httpStatus"),
                probe.get("ok"),
            )
            return
        cur = body.get("current_node")
        mapping_obj = body.get("mapping")
        mapping: dict[str, Any] = mapping_obj if isinstance(mapping_obj, dict) else {}
        node = mapping.get(cur) if isinstance(cur, str) else None
        msg = node.get("message") if isinstance(node, dict) else None
        if not isinstance(msg, dict):
            logger.debug(
                "chatgpt_web gate-diag poll=%s cur=%s no-message nodes=%s", polls, cur, len(mapping)
            )
            return
        author = msg.get("author") or {}
        meta = msg.get("metadata") or {}
        content = msg.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else None
        text = "".join(p for p in parts if isinstance(p, str)) if isinstance(parts, list) else ""
        sentinel = f"END_REVIEW:{run_id}:{bundle_sha}"
        logger.debug(
            "chatgpt_web gate-diag poll=%s role=%s status=%s end_turn=%s model=%s effort=%s "
            "answer_len=%s sentinel_present=%s submitted_uid=%s cur=%s",
            polls,
            author.get("role") if isinstance(author, dict) else None,
            msg.get("status"),
            msg.get("end_turn"),
            meta.get("model_slug") if isinstance(meta, dict) else None,
            meta.get("thinking_effort") if isinstance(meta, dict) else None,
            len(text),
            sentinel in text,
            submitted_user_msg_id[:12],
            str(cur)[:16],
        )
        # On a FINISHED assistant node missing the sentinel, log the answer TAIL
        # once (non-secret: it is the model's own findings text) to diagnose
        # prompt-adherence — never a token.
        role = author.get("role") if isinstance(author, dict) else None
        if role == "assistant" and msg.get("end_turn") and sentinel not in text:
            logger.debug("chatgpt_web gate-diag answer_tail=%r", text[-240:])
