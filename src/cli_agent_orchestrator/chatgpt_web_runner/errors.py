"""Typed error taxonomy and delivery classification (D4, D7).

The runner never raises a bare string. Every failure is a :class:`RunnerError`
carrying a :class:`RunnerErrorCode` from the closed taxonomy, a redacted hint,
and the four-way :class:`DeliveryState`. The provider maps a code onto the
condition plane (``providers/condition.py``) and the 6-value ``TerminalStatus``;
the code itself is transport-agnostic so AC-7/AC-8 assert against a fixed shape
rather than log text.

D4 mapping (code -> condition/status intent, resolved in the provider, NOT here):
    auth_wall          -> AUTH_EXPIRED           (human-gated)
    captcha            -> DIALOG_BLOCKED/captcha  (human-gated)
    bot_flagged        -> DIALOG_BLOCKED subtype bot_flagged -> WAITING_USER_ANSWER
    quota              -> CAPPED (observed reset hint only)
    ui_changed         -> ERROR
    model_drift        -> ERROR
    truncated_answer   -> ERROR
    invalid_verdict    -> ERROR
    submit_unknown     -> ERROR
    context_too_large  -> CONTEXT_EXHAUSTED
    net_interrupted    -> NET_INTERRUPTED (reconnect + re-observe SAME conv once)
    proc_exited        -> PROC_EXITED
    access_denied      -> ERROR (unknown 403 fails closed, human-gated)
"""

from __future__ import annotations

from enum import Enum


class RunnerErrorCode(str, Enum):
    """Closed error taxonomy (D4). Values are stable wire strings."""

    # --- Auth / anti-bot / quota (human-gated in the provider) -------------
    AUTH_WALL = "auth_wall"
    CAPTCHA = "captcha"
    BOT_FLAGGED = "bot_flagged"
    ACCESS_DENIED = "access_denied"
    QUOTA = "quota"

    # --- Transport / delivery ----------------------------------------------
    SUBMIT_UNKNOWN = "submit_unknown"
    NET_INTERRUPTED = "net_interrupted"
    PROC_EXITED = "proc_exited"

    # --- Answer integrity ---------------------------------------------------
    UI_CHANGED = "ui_changed"
    MODEL_DRIFT = "model_drift"
    TRUNCATED_ANSWER = "truncated_answer"
    INVALID_VERDICT = "invalid_verdict"

    # --- Input framing ------------------------------------------------------
    CONTEXT_TOO_LARGE = "context_too_large"
    UPLOAD_UNCONFIRMED = "upload_unconfirmed"
    ATTACH_TIMEOUT = "attach_timeout"
    ATTACHMENT_IDENTITY = "attachment_identity"
    PIN_DRIFT = "pin_drift"

    # --- Read-exception containment (D3/AC-11b) ----------------------------
    READ_FORBIDDEN = "read_forbidden"
    EGRESS_FORBIDDEN = "egress_forbidden"

    # --- Report integrity ---------------------------------------------------
    REPORT_INVALID = "report_invalid"


class DeliveryState(str, Enum):
    """Four-way delivery classification (D7), verbatim from r1 §5.

    * ``NOTHING_SENT``  — failure demonstrably BEFORE Enter/click dispatch
      (local validation or upload failure). One retry may be safe after
      correction.
    * ``ACK_UNKNOWN``   — submit was attempted but no correlated server or
      user-turn evidence arrived. Reconcile the SAME conversation, never
      blindly resend.
    * ``DELIVERED``     — a matching user node or server acceptance tied to its
      ID/payload was observed. Recover and READ only, even if the answer or
      conversation discovery times out.
    * ``TIMEOUT``       — deadline exhausted, carrying the last delivery state
      and any known IDs. No automatic resend; no partial findings promoted to
      success.
    """

    NOTHING_SENT = "nothing-sent"
    ACK_UNKNOWN = "ack-unknown"
    DELIVERED = "delivered"
    TIMEOUT = "timeout"


#: The ONLY delivery state for which a retry (after correction) may be safe
#: (D7/AC-8). ``ack-unknown``, ``delivered`` and ``timeout`` never resend.
RETRYABLE_DELIVERY_STATES: frozenset[DeliveryState] = frozenset({DeliveryState.NOTHING_SENT})


def delivery_state_may_retry(state: DeliveryState) -> bool:
    """True iff a fresh send after correction is permitted for ``state`` (D7)."""
    return state in RETRYABLE_DELIVERY_STATES


class RunnerError(Exception):
    """A typed runner failure with a redacted hint and delivery classification.

    ``hint`` MUST NOT carry a cookie/bearer/sentinel value or a full pane dump —
    it is a short human-readable string that reaches the envelope and logs
    (AC-11). Construct with :meth:`redacted` when the source text is untrusted.
    """

    def __init__(
        self,
        code: RunnerErrorCode,
        hint: str = "",
        *,
        delivery_state: DeliveryState = DeliveryState.NOTHING_SENT,
    ) -> None:
        self.code = code
        self.hint = _scrub_hint(hint)
        self.delivery_state = delivery_state
        super().__init__(f"{code.value}: {self.hint}")

    @classmethod
    def redacted(
        cls,
        code: RunnerErrorCode,
        raw: str,
        *,
        delivery_state: DeliveryState = DeliveryState.NOTHING_SENT,
        max_len: int = 200,
    ) -> "RunnerError":
        """Build an error whose hint is scrubbed and length-capped."""
        return cls(code, _scrub_hint(raw)[:max_len], delivery_state=delivery_state)


# Substrings whose PRESENCE means a token-bearing value may be in the text; if
# any is found the hint is replaced wholesale rather than partially masked, so a
# novel token shape can never slip through a regex gap (fail closed, AC-11).
_SECRET_MARKERS: tuple[str, ...] = (
    "authorization",
    "bearer ",
    "cookie:",
    "set-cookie",
    "accesstoken",
    "access_token",
    "openai-sentinel",
    "x-conduit-token",
    "proof-token",
    "turnstile-token",
    "__secure-",
    "session-token",
    "eyj",  # a JWT always starts base64url "eyJ"
)


def _scrub_hint(text: str) -> str:
    """Return ``text`` unless it looks like it may carry a secret, else a marker.

    This is a fail-closed scrub: any hint that contains a known secret marker
    (case-insensitive) is dropped entirely and replaced with a fixed string. It
    is the last line of defence for AC-11 — the runner never intentionally puts
    a token in a hint, and this guarantees a mistake cannot leak one.
    """
    if not text:
        return ""
    low = text.lower()
    for marker in _SECRET_MARKERS:
        if marker in low:
            return "[redacted: hint contained a token-shaped value]"
    return text
