"""F970 (#819) step 3 — the detached read and the detached upload chain.

Both are r4-measured capabilities, and they are deliberately at different
maturity levels:

* **The read is wired into the lane.** The authoritative conversation GET
  succeeds from a plain Python process on the exported session alone (probes
  §7), so the poll no longer needs a live page. :class:`DetachedReader` returns
  the SAME dict shape the in-page fetch returns, which is what lets
  ``Transport.read_conversation`` swap transports without the gate, the poll
  loop or their tests knowing anything changed. On any failure the transport
  falls back to the in-page read for the rest of the turn.

* **The upload chain is built and tested, NOT wired in.** r4 settled it end to
  end twice (probes §8): allocate same-origin, PUT the bytes to the signed
  ``oaiusercontent.com`` destination, complete, then poll until ``state ==
  "ready"``. But Amendment C's AC-9 replacement requires *"a live A send
  consuming that exact uploaded reference before this mechanism replaces
  current A attachment certification"*, and shape A's send is the app's own
  composer — which attaches the file IT uploaded, not a file id we allocated.
  Consuming an uploaded reference therefore requires the Python send (B′),
  which Amendment D does not authorise. So this ships as a capability with its
  protocol pinned by tests, and the composer attach path is untouched.

The readiness correction from r4 is load-bearing and encoded here: the
authoritative predicate is the ``state`` field on the file record — NOT
``retrieval_index_status`` and NOT the ``status`` returned by the completion
call. A first pass keyed on those polled 27 times while the file was long
ready.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from cli_agent_orchestrator.chatgpt_web_runner.detached_http import (
    BASE_ORIGIN,
    assert_no_session_auth,
    assert_upload_destination,
    base_headers,
    build_session,
    upload_headers,
)
from cli_agent_orchestrator.chatgpt_web_runner.errors import (
    DeliveryState,
    RunnerError,
    RunnerErrorCode,
)
from cli_agent_orchestrator.chatgpt_web_runner.session_export import SessionBundle
from cli_agent_orchestrator.chatgpt_web_runner.snapshot_upload import enforce_read_allowed

logger = logging.getLogger(__name__)

#: Readiness predicate for an uploaded file (r4 §8 correction).
READY_STATES: frozenset[str] = frozenset({"ready"})


class DetachedReader:
    """The authoritative conversation GET, issued without a page.

    Returns the in-page transport's dict shape verbatim —
    ``{httpStatus, ok, retryAfter, body}`` — so the poll loop and the D6 gate
    are transport-agnostic. Headers, cookies and the bearer never appear in the
    return value (AC-11).
    """

    def __init__(
        self,
        bundle: SessionBundle,
        *,
        session: Optional[Any] = None,
        template: Optional[str] = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.bundle = bundle
        self.timeout_s = timeout_s
        # Constructing the session runs the C5 template assertion first; an
        # invalid template raises TlsTemplateUnavailable here, before traffic.
        self.session = session if session is not None else build_session(bundle, template=template)

    def read_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """GET one owned conversation. Containment is checked before the call."""
        url = f"{BASE_ORIGIN}/backend-api/conversation/{conversation_id}"
        # Same bound as the in-page path: own conversation only, no enumeration.
        enforce_read_allowed(url, conversation_id)
        resp = self.session.get(url, headers=base_headers(self.bundle), timeout=self.timeout_s)
        status = int(getattr(resp, "status_code", 0) or 0)
        retry_after = None
        try:
            raw_retry = resp.headers.get("retry-after")
            retry_after = float(raw_retry) if raw_retry is not None else None
        except (AttributeError, TypeError, ValueError):
            retry_after = None
        out: Dict[str, Any] = {
            "httpStatus": status,
            "ok": 200 <= status < 300,
            "retryAfter": retry_after,
            "body": None,
        }
        if not out["ok"]:
            return out
        try:
            out["body"] = resp.json()
        except Exception:
            try:
                out["body"] = json.loads(resp.text)
            except Exception:
                out["body"] = None
        return out

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover - best-effort
            pass


@dataclass(frozen=True)
class UploadedFile:
    """The final server reference for an uploaded attachment (r4 §8)."""

    file_id: str
    byte_length: int
    sha256: str
    state: str
    polls: int
    elapsed_ms: int


class DetachedUploader:
    """The four-stage HTTP upload chain, browser-free.

    Built and protocol-tested; NOT wired into the review lane's attach path —
    see the module docstring for why (AC-9 replacement needs a send that
    consumes the reference, i.e. B′).
    """

    def __init__(
        self,
        bundle: SessionBundle,
        *,
        session: Optional[Any] = None,
        template: Optional[str] = None,
        timeout_s: float = 45.0,
        transfer_timeout_s: float = 90.0,
        readiness_timeout_s: float = 60.0,
        sleep: Any = time.sleep,
    ) -> None:
        self.bundle = bundle
        self.timeout_s = timeout_s
        self.transfer_timeout_s = transfer_timeout_s
        self.readiness_timeout_s = readiness_timeout_s
        self._sleep = sleep
        self.session = session if session is not None else build_session(bundle, template=template)

    # ── stages ───────────────────────────────────────────────────────────

    def _allocate(self, filename: str, size: int) -> Dict[str, Any]:
        resp = self.session.post(
            f"{BASE_ORIGIN}/backend-api/files",
            headers=base_headers(self.bundle, {"content-type": "application/json"}),
            json={
                "file_name": filename,
                "file_size": size,
                "use_case": "multimodal",
                "reset_rate_limits": False,
            },
            timeout=self.timeout_s,
        )
        status = int(getattr(resp, "status_code", 0) or 0)
        if status != 200:
            raise RunnerError(
                RunnerErrorCode.UPLOAD_UNCONFIRMED,
                f"file allocation failed with HTTP {status}",
                delivery_state=DeliveryState.NOTHING_SENT,
            )
        body = resp.json()
        if not isinstance(body, dict) or not body.get("file_id") or not body.get("upload_url"):
            raise RunnerError(
                RunnerErrorCode.UPLOAD_UNCONFIRMED,
                "file allocation response carried no file id / upload url",
                delivery_state=DeliveryState.NOTHING_SENT,
            )
        return body

    def _transfer(self, upload_url: str, data: bytes, content_type: str) -> None:
        # C5: exactly the signed destination named by THIS allocation, with no
        # session auth attached and no redirect followed.
        assert_upload_destination(upload_url)
        headers = upload_headers(content_type)
        assert_no_session_auth(headers)
        resp = self.session.put(
            upload_url,
            data=data,
            headers=headers,
            timeout=self.transfer_timeout_s,
            allow_redirects=False,
        )
        status = int(getattr(resp, "status_code", 0) or 0)
        if status in (301, 302, 303, 307, 308):
            raise RunnerError(
                RunnerErrorCode.EGRESS_FORBIDDEN,
                "signed upload destination attempted a redirect",
                delivery_state=DeliveryState.NOTHING_SENT,
            )
        if status not in (200, 201):
            raise RunnerError(
                RunnerErrorCode.UPLOAD_UNCONFIRMED,
                f"byte transfer failed with HTTP {status}",
                delivery_state=DeliveryState.NOTHING_SENT,
            )

    def _complete(self, file_id: str) -> None:
        resp = self.session.post(
            f"{BASE_ORIGIN}/backend-api/files/{file_id}/uploaded",
            headers=base_headers(self.bundle, {"content-type": "application/json"}),
            json={},
            timeout=self.timeout_s,
        )
        status = int(getattr(resp, "status_code", 0) or 0)
        if status != 200:
            raise RunnerError(
                RunnerErrorCode.UPLOAD_UNCONFIRMED,
                f"upload completion failed with HTTP {status}",
                delivery_state=DeliveryState.NOTHING_SENT,
            )

    def _await_ready(self, file_id: str) -> Dict[str, Any]:
        """Poll until ``state == "ready"``. Completion success is NOT readiness."""
        started = time.monotonic()
        polls = 0
        last: Dict[str, Any] = {}
        while time.monotonic() - started < self.readiness_timeout_s:
            polls += 1
            resp = self.session.get(
                f"{BASE_ORIGIN}/backend-api/files/{file_id}",
                headers=base_headers(self.bundle),
                timeout=self.timeout_s,
            )
            if int(getattr(resp, "status_code", 0) or 0) == 200:
                try:
                    last = resp.json()
                except Exception:
                    last = {}
                if isinstance(last, dict) and str(last.get("state") or "") in READY_STATES:
                    return {
                        "state": str(last.get("state")),
                        "polls": polls,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                    }
            self._sleep(2.0)
        raise RunnerError(
            RunnerErrorCode.UPLOAD_UNCONFIRMED,
            f"uploaded file never reached state=ready after {polls} polls",
            delivery_state=DeliveryState.NOTHING_SENT,
        )

    # ── the chain ────────────────────────────────────────────────────────

    def upload(self, path: str, *, content_type: str = "text/plain") -> UploadedFile:
        """Run allocate → transfer → complete → readiness and return the reference."""
        data = Path(path).read_bytes()
        filename = Path(path).name
        started = time.monotonic()
        allocation = self._allocate(filename, len(data))
        file_id = str(allocation["file_id"])
        self._transfer(str(allocation["upload_url"]), data, content_type)
        self._complete(file_id)
        ready = self._await_ready(file_id)
        result = UploadedFile(
            file_id=file_id,
            byte_length=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            state=ready["state"],
            polls=int(ready["polls"]),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        logger.info(
            "chatgpt_web detached upload ready: %s bytes, %s polls, %s ms",
            result.byte_length,
            result.polls,
            result.elapsed_ms,
        )
        return result

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover - best-effort
            pass
