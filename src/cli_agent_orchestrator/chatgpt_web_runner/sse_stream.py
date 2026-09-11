"""F970 (#819) — consume the app's own send SSE (delta encoding v1).

F862 shipped shape A with the send stream **observed by URL only**: D6 reads
``resp.url`` on ``POST /backend-api/f/conversation`` to learn that a send
happened, and the body is never read. Blueprint Amendment D (2026-09-11)
reopens that: the user's rule is *"use API drive whenever we can … granular
control (stream SSE, forward it)"*, and a forwardable token stream is the
operational benefit Amendment C's reopening condition #1 asks for.

**What changes and what does not.** This module reads a TEE of the response the
app itself received — it forges nothing, issues no request, and re-sends
nothing. D6 keeps its authority in full: the conversation GET remains the
authoritative completion and the accepted answer; everything produced here is
progress, quota and early-failure signal. A partial from this stream is never
promoted to an accepted answer.

Wire shape (findings ``chatgpt-api-behavior.md`` §2, "Stream event shapes"):

* the first frame is ``{event:"delta_encoding", data:"v1"}``;
* then ``event:"delta"`` frames whose ``data`` is a JSON patch
  ``{p:<json-pointer>, o:"add"|"append"|"patch"|"replace", v:<value>}`` against
  a ``message`` object — content arrives as
  ``{p:"/message/content/parts/0", o:"append", v:"<text chunk>"}``, and a frame
  that carries only ``v`` CONTINUES the previous pointer/op;
* interleaved ``data.type`` frames carry ``conversation_detail_metadata``
  (whose ``limits_progress`` is the quota read: ``[{feature_name, remaining,
  reset_after}]``), ``message_stream_complete``, ``title_generation``,
  ``message_marker``, ``server_ste_metadata`` and ``resume_conversation_token``;
* the stream closes with ``data:"[DONE]"``.

**Hygiene is an allow-list, not a scrub (AC-11).** Two frames on this stream are
known to carry credential-shaped values — ``resume_conversation_token`` is a
JWT. Rather than pattern-matching them out afterwards, the tracker interprets
ONLY an enumerated set of JSON pointers and typed-event fields and discards
every other byte, so a *new* token-bearing field added by OpenAI tomorrow is
dropped by default instead of being stored until someone notices. The in-page
tee applies the same rule one layer earlier: a chunk line naming the resume
token never leaves the page at all.

The parser is pure (no Playwright import) so the recorded-fixture tests run
offline; :func:`build_sse_tee_script` returns the in-page JS the transport
installs.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: The page-side callback name the tee pushes decoded chunks into. Exposed from
#: Python with ``page.expose_function``.
SSE_BINDING_NAME = "__caoSseChunk"

#: The send endpoint whose body we tee (the ``/prepare`` pre-warm is excluded —
#: it is not the send; ``submit_ids.is_send_response_url`` owns the same rule).
SEND_PATH = "/backend-api/f/conversation"

# --- the interpretation allow-list -------------------------------------------

#: Content text. ``append`` (the streaming case), plus ``add``/``replace`` for a
#: buffered first chunk.
PTR_CONTENT_PART0 = "/message/content/parts/0"
PTR_STATUS = "/message/status"
PTR_END_TURN = "/message/end_turn"
PTR_MODEL_SLUG = "/message/metadata/model_slug"
PTR_RESOLVED_MODEL = "/message/metadata/resolved_model_slug"
PTR_THINKING_EFFORT = "/message/metadata/thinking_effort"

#: Every pointer this module will act on. Anything else — including a pointer
#: that appears in a future app build — is remembered only as "the last pointer"
#: (so its continuation frames stay ignored too) and never stored.
ALLOWED_POINTERS: frozenset[str] = frozenset(
    {
        PTR_CONTENT_PART0,
        PTR_STATUS,
        PTR_END_TURN,
        PTR_MODEL_SLUG,
        PTR_RESOLVED_MODEL,
        PTR_THINKING_EFFORT,
    }
)

#: Typed (``data.type``) frames this module interprets. ``resume_conversation_token``
#: is deliberately ABSENT: it carries a JWT.
TYPE_CONVERSATION_DETAIL = "conversation_detail_metadata"
TYPE_STREAM_COMPLETE = "message_stream_complete"
ALLOWED_TYPES: frozenset[str] = frozenset({TYPE_CONVERSATION_DETAIL, TYPE_STREAM_COMPLETE})

#: The only keys kept from a ``limits_progress`` entry (findings §6).
QUOTA_KEYS: Tuple[str, ...] = ("feature_name", "remaining", "reset_after")

#: A frame naming any of these is dropped whole, wherever it appears. The
#: allow-list above already excludes them; this is the belt to that suspenders,
#: and it is what the in-page tee filters on so the value never crosses the
#: process boundary in the first place.
TOKEN_MARKERS: Tuple[str, ...] = (
    "token",  # subsumes resume_conversation_token / access_token / conduit_token
    "authorization",
    "cookie",
    "sentinel",
    "turnstile",
    "credential",
    "secret",
)

#: Event kinds emitted by the tracker.
KIND_ENCODING = "encoding"
KIND_TOKEN = "token"
KIND_STATUS = "status"
KIND_METADATA = "metadata"
KIND_QUOTA = "quota"
KIND_STREAM_COMPLETE = "stream_complete"
KIND_DONE = "done"


@dataclass(frozen=True)
class StreamEvent:
    """One interpreted progress event. Carries only allow-listed data.

    ``kind`` is one of the ``KIND_*`` constants. ``text`` is set for
    ``token``; ``field``/``value`` for ``status`` and ``metadata``; ``quota``
    for ``quota``. ``seq`` is a per-turn monotonic counter so a relay can
    address events without inventing its own ordering.
    """

    kind: str
    seq: int = 0
    text: str = ""
    field_name: str = ""
    value: Any = None
    quota: Tuple[Dict[str, Any], ...] = ()
    conversation_id: str = ""

    def to_payload(self) -> Dict[str, Any]:
        """A JSON-safe dict for a relay/log. Non-secret by construction."""
        out: Dict[str, Any] = {"kind": self.kind, "seq": self.seq}
        if self.text:
            out["text"] = self.text
        if self.field_name:
            out["field"] = self.field_name
            out["value"] = self.value
        if self.quota:
            out["quota"] = [dict(q) for q in self.quota]
        if self.conversation_id:
            out["conversation_id"] = self.conversation_id
        return out


@dataclass
class _Frame:
    event: Optional[str]
    data: str


def iter_sse_frames(buffer: str) -> Tuple[List[_Frame], str]:
    """Split ``buffer`` into complete SSE frames plus the trailing remainder.

    Chunk boundaries fall anywhere — mid-frame, mid-line, mid-UTF-8-sequence
    (the decoder upstream handles the last case) — so the caller feeds the
    remainder back in with the next chunk.
    """
    frames: List[_Frame] = []
    normalized = buffer.replace("\r\n", "\n")
    parts = normalized.split("\n\n")
    remainder = parts.pop() if parts else ""
    for raw in parts:
        event: Optional[str] = None
        data_lines: List[str] = []
        for line in raw.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
            # ``id:``/``retry:``/comments are not used by this stream.
        if data_lines or event:
            frames.append(_Frame(event=event, data="\n".join(data_lines)))
    return frames, remainder


#: A JSON KEY naming one of the markers, e.g. ``"resume_conversation_token":``.
#: Matching on keys — not on the raw bytes — matters: the answer TEXT itself
#: routinely contains words like "sentinel" (this lane's own END_REVIEW framing
#: is called a sentinel), and a raw-substring rule would silently drop content
#: frames and leave a truncated stream that looks like a model failure.
_TOKEN_KEY_RE = re.compile(
    r'"[a-z0-9_.\-]*(?:' + "|".join(TOKEN_MARKERS) + r')[a-z0-9_.\-]*"\s*:', re.IGNORECASE
)


def _looks_token_bearing(raw: str) -> bool:
    """True when a raw frame carries a JSON KEY naming a credential-shaped field."""
    return bool(_TOKEN_KEY_RE.search(raw))


def _clean_quota(entries: Any) -> Tuple[Dict[str, Any], ...]:
    """Project ``limits_progress`` onto the three allow-listed keys."""
    if not isinstance(entries, list):
        return ()
    out: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kept = {k: entry.get(k) for k in QUOTA_KEYS if k in entry}
        if kept:
            out.append(kept)
    return tuple(out)


class SseProgressTracker:
    """Accumulates allow-listed progress from the teed send stream.

    Stateful across chunks and frames; never raises on malformed input (a
    stream this cannot parse must degrade to "no progress", never to a failed
    turn — the conversation GET is what decides the turn).
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._seq = 0
        self._last_pointer: Optional[str] = None
        self._last_op: Optional[str] = None
        # --- accumulated, allow-listed state -----------------------------
        self.text: str = ""
        self.status: Optional[str] = None
        self.end_turn: Optional[bool] = None
        self.model_slug: Optional[str] = None
        self.resolved_model_slug: Optional[str] = None
        self.thinking_effort: Optional[str] = None
        self.conversation_id: str = ""
        self.quota: Tuple[Dict[str, Any], ...] = ()
        self.encoding: Optional[str] = None
        self.stream_complete: bool = False
        self.done: bool = False
        self.token_events: int = 0
        self.frames_seen: int = 0
        self.frames_dropped: int = 0

    # ── feeding ──────────────────────────────────────────────────────────

    def feed(self, chunk: str) -> List[StreamEvent]:
        """Consume one decoded chunk; return the events it completed."""
        if not chunk:
            return []
        self._buffer += chunk
        frames, self._buffer = iter_sse_frames(self._buffer)
        events: List[StreamEvent] = []
        for frame in frames:
            events.extend(self._handle_frame(frame))
        return events

    def _emit(self, **kwargs: Any) -> StreamEvent:
        self._seq += 1
        return StreamEvent(seq=self._seq, **kwargs)

    def _handle_frame(self, frame: _Frame) -> List[StreamEvent]:
        self.frames_seen += 1
        data = frame.data.strip()
        if not data:
            return []
        if data == "[DONE]":
            self.done = True
            return [self._emit(kind=KIND_DONE)]
        if _looks_token_bearing(data):
            # Fail closed: a frame naming a credential-shaped field is dropped
            # whole, before any parse. Nothing from it reaches state or events.
            self.frames_dropped += 1
            return []
        if frame.event == "delta_encoding":
            try:
                self.encoding = json.loads(data)
            except ValueError:
                self.encoding = data.strip('"')
            return [
                self._emit(kind=KIND_ENCODING, field_name="delta_encoding", value=self.encoding)
            ]
        try:
            parsed = json.loads(data)
        except ValueError:
            self.frames_dropped += 1
            return []
        if isinstance(parsed, dict) and isinstance(parsed.get("type"), str):
            return self._handle_typed(parsed)
        return self._apply_patch(parsed)

    # ── typed (``data.type``) frames ─────────────────────────────────────

    def _handle_typed(self, parsed: Dict[str, Any]) -> List[StreamEvent]:
        kind = parsed.get("type")
        if kind not in ALLOWED_TYPES:
            self.frames_dropped += 1
            return []
        conv = parsed.get("conversation_id")
        if isinstance(conv, str) and conv:
            self.conversation_id = conv
        if kind == TYPE_CONVERSATION_DETAIL:
            quota = _clean_quota(parsed.get("limits_progress"))
            events: List[StreamEvent] = []
            if quota:
                self.quota = quota
                events.append(
                    self._emit(kind=KIND_QUOTA, quota=quota, conversation_id=self.conversation_id)
                )
            default_slug = parsed.get("default_model_slug")
            if isinstance(default_slug, str) and default_slug and not self.model_slug:
                events.append(
                    self._emit(
                        kind=KIND_METADATA, field_name="default_model_slug", value=default_slug
                    )
                )
            return events
        # message_stream_complete
        self.stream_complete = True
        return [self._emit(kind=KIND_STREAM_COMPLETE, conversation_id=self.conversation_id)]

    # ── delta patches ────────────────────────────────────────────────────

    def _apply_patch(self, patch: Any) -> List[StreamEvent]:
        if not isinstance(patch, dict):
            return []
        op = patch.get("o")
        pointer = patch.get("p")
        value = patch.get("v")

        if op == "patch" and isinstance(value, list):
            events: List[StreamEvent] = []
            for sub in value:
                events.extend(self._apply_patch(sub))
            return events

        if pointer is None and op is None:
            # Continuation frame: the previous pointer/op still apply. This is
            # why a NON-allow-listed pointer must also be remembered — otherwise
            # its continuations would fall through onto the last allowed one and
            # a status string would be appended to the answer text.
            pointer, op = self._last_pointer, self._last_op or "append"
        elif pointer is not None:
            self._last_pointer = pointer if isinstance(pointer, str) else None
            self._last_op = op if isinstance(op, str) else None

        if pointer == "" and isinstance(value, dict):
            # The opening frame adds the whole message object.
            return self._absorb_message_object(value)

        if not isinstance(pointer, str) or pointer not in ALLOWED_POINTERS:
            self.frames_dropped += 1
            return []
        return self._apply_allowed(pointer, op, value)

    def _apply_allowed(self, pointer: str, op: Any, value: Any) -> List[StreamEvent]:
        if pointer == PTR_CONTENT_PART0:
            if not isinstance(value, str):
                return []
            if op == "append":
                self.text += value
            else:  # add / replace — a buffered or corrected first chunk
                self.text = value
            self.token_events += 1
            return [self._emit(kind=KIND_TOKEN, text=value)]
        if pointer == PTR_STATUS:
            self.status = value if isinstance(value, str) else self.status
            return [self._emit(kind=KIND_STATUS, field_name="status", value=self.status)]
        if pointer == PTR_END_TURN:
            self.end_turn = bool(value) if value is not None else None
            return [self._emit(kind=KIND_STATUS, field_name="end_turn", value=self.end_turn)]
        if pointer == PTR_MODEL_SLUG:
            self.model_slug = value if isinstance(value, str) else self.model_slug
            return [self._emit(kind=KIND_METADATA, field_name="model_slug", value=self.model_slug)]
        if pointer == PTR_RESOLVED_MODEL:
            self.resolved_model_slug = value if isinstance(value, str) else self.resolved_model_slug
            return [
                self._emit(
                    kind=KIND_METADATA,
                    field_name="resolved_model_slug",
                    value=self.resolved_model_slug,
                )
            ]
        if pointer == PTR_THINKING_EFFORT:
            self.thinking_effort = value if isinstance(value, str) else self.thinking_effort
            return [
                self._emit(
                    kind=KIND_METADATA, field_name="thinking_effort", value=self.thinking_effort
                )
            ]
        return []

    def _absorb_message_object(self, value: Dict[str, Any]) -> List[StreamEvent]:
        """Read the allow-listed fields out of the opening whole-message add."""
        events: List[StreamEvent] = []
        conv = value.get("conversation_id")
        if isinstance(conv, str) and conv:
            self.conversation_id = conv
        message = value.get("message")
        if not isinstance(message, dict):
            return events
        content = message.get("content")
        if isinstance(content, dict):
            parts = content.get("parts")
            if isinstance(parts, list) and parts and isinstance(parts[0], str) and parts[0]:
                self.text = parts[0]
                self.token_events += 1
                events.append(self._emit(kind=KIND_TOKEN, text=parts[0]))
        status = message.get("status")
        if isinstance(status, str):
            self.status = status
        meta = message.get("metadata")
        if isinstance(meta, dict):
            for key, attr in (
                ("model_slug", "model_slug"),
                ("resolved_model_slug", "resolved_model_slug"),
                ("thinking_effort", "thinking_effort"),
            ):
                got = meta.get(key)
                if isinstance(got, str) and got:
                    setattr(self, attr, got)
                    events.append(self._emit(kind=KIND_METADATA, field_name=key, value=got))
        return events

    # ── read-out ─────────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """A non-secret summary for the envelope/log."""
        return {
            "encoding": self.encoding,
            "token_events": self.token_events,
            "text_len": len(self.text),
            "status": self.status,
            "end_turn": self.end_turn,
            "model_slug": self.model_slug,
            "thinking_effort": self.thinking_effort,
            "conversation_id": self.conversation_id,
            "quota": [dict(q) for q in self.quota],
            "stream_complete": self.stream_complete,
            "done": self.done,
            "frames_seen": self.frames_seen,
            "frames_dropped": self.frames_dropped,
        }


def build_sse_tee_script(binding_name: str = SSE_BINDING_NAME) -> str:
    """Return the init script that tees the send response body in-page.

    It wraps ``window.fetch`` and, for the send response only, reads
    ``response.clone()`` — the browser's own tee. The app keeps the ORIGINAL
    response object untouched (a re-wrapped ``Response`` would lose ``url`` and
    ``redirected`` and could change app behaviour), and **no request is ever
    issued by this code**: there is exactly one call through to the original
    fetch, the one the app asked for. That is what keeps the D7/D16 one-send
    invariant intact while the body is read.

    Lines naming ``resume_conversation_token`` are dropped in-page, so the JWT
    on that frame never crosses into the Python process.
    """
    name = json.dumps(binding_name)
    send_path = json.dumps(SEND_PATH)
    return (
        "(() => {\n"
        "  if (window.__caoSseTeeInstalled) return;\n"
        "  window.__caoSseTeeInstalled = true;\n"
        f"  const SEND = {send_path};\n"
        f"  const SINK = {name};\n"
        "  const origFetch = window.fetch;\n"
        "  window.fetch = function () {\n"
        "    const args = arguments;\n"
        "    const input = args[0];\n"
        "    let url = '';\n"
        "    try { url = (typeof input === 'string') ? input : ((input && input.url) || ''); }\n"
        "    catch (e) { url = ''; }\n"
        "    const p = origFetch.apply(this, args);\n"
        "    try {\n"
        "      const isSend = url.indexOf(SEND) !== -1 && url.indexOf('/prepare') === -1;\n"
        "      if (!isSend) return p;\n"
        "      return p.then((res) => {\n"
        "        try {\n"
        "          const ct = (res.headers && res.headers.get)"
        " ? (res.headers.get('content-type') || '') : '';\n"
        "          if (ct.indexOf('text/event-stream') === -1 || !res.body) return res;\n"
        "          const copy = res.clone();\n"
        "          (async () => {\n"
        "            const reader = copy.body.getReader();\n"
        "            const dec = new TextDecoder();\n"
        "            for (;;) {\n"
        "              const r = await reader.read();\n"
        "              if (r.done) break;\n"
        "              let chunk = dec.decode(r.value, {stream: true});\n"
        "              if (chunk.indexOf('resume_conversation_token') !== -1) {\n"
        "                chunk = chunk.split('\\n').filter("
        "(l) => l.indexOf('resume_conversation_token') === -1).join('\\n');\n"
        "              }\n"
        "              if (chunk && window[SINK]) { try { window[SINK](chunk); } catch (e) {} }\n"
        "            }\n"
        "          })();\n"
        "        } catch (e) {}\n"
        "        return res;\n"
        "      });\n"
        "    } catch (e) { return p; }\n"
        "  };\n"
        "})()"
    )
