"""Submit correlation, conversation-id discovery, and delivery classification.

Pure logic ported from the bun spike's four-way submit-confirm
(``ask.ts:684-727``) and conversation-id regex (``ask.ts:50``). No browser
import here — the transport module feeds this observed facts and it returns the
:class:`DeliveryState`. This keeps AC-8's four-way discrimination unit-testable
with plain inputs (grok-web's injectable pattern).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Optional

from cli_agent_orchestrator.chatgpt_web_runner.errors import DeliveryState

#: A chatgpt.com conversation URL carries ``/c/<id>`` (ask.ts:50). On the Plus
#: account the id observed live in the F862 spike is ``WEB:<uuid>`` (an optional
#: uppercase source-prefix before the uuid), and the conversation GET path uses
#: that id VERBATIM — so the capture keeps the whole ``[A-Za-z]+:``-prefixed
#: token, not just the bare uuid. A plain ``<uuid>`` (no prefix) still matches.
CONVERSATION_ID_RE = re.compile(
    r"/c/((?:[A-Za-z]+:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)

#: The frontend send endpoint (findings §2). The ``/prepare`` pre-warm is
#: EXCLUDED — it is not the send (ask.ts:633-645).
SEND_ENDPOINT_SUBSTR = "/backend-api/f/conversation"
SEND_ENDPOINT_PREPARE_SUFFIX = "/prepare"


def extract_conversation_id(url: str) -> Optional[str]:
    """Return the ``/c/<uuid>`` conversation id in ``url``, or None."""
    match = CONVERSATION_ID_RE.search(url or "")
    return match.group(1) if match else None


def is_send_response_url(url: str) -> bool:
    """True iff ``url`` is the load-bearing send (not the /prepare pre-warm)."""
    u = url or ""
    if SEND_ENDPOINT_SUBSTR not in u:
        return False
    return not u.rstrip("/").endswith(
        "f/conversation" + SEND_ENDPOINT_PREPARE_SUFFIX
    ) and not u.endswith(SEND_ENDPOINT_SUBSTR + SEND_ENDPOINT_PREPARE_SUFFIX)


def new_run_id() -> str:
    """Mint a fresh run id used in the END_REVIEW sentinel and manifest."""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class SubmitObservation:
    """Facts observed in the submit-confirm window, fed to the classifier.

    Every field is a plain observation the transport layer records; the
    classifier never touches the browser.

    * ``enter_dispatched`` — True once the Enter keypress was issued. A failure
      BEFORE this (local validation, upload) is ``nothing-sent``.
    * ``pre_validation_failed`` — a local/upload failure happened before Enter.
    * ``new_user_turn`` — a user-role turn beyond the pre-submit count appeared.
    * ``send_response_seen`` — the ``/f/conversation`` (non-/prepare) response
      was observed off the wire.
    * ``conversation_id`` — resolved ``/c/<uuid>`` (from URL or SSE), if any.
    * ``composer_cleared`` — the composer no longer holds the prompt text.
    * ``deadline_exhausted`` — the submit-confirm window elapsed with no
      confirming signal.
    """

    enter_dispatched: bool = False
    pre_validation_failed: bool = False
    new_user_turn: bool = False
    send_response_seen: bool = False
    conversation_id: Optional[str] = None
    composer_cleared: bool = False
    deadline_exhausted: bool = False


def classify_delivery(obs: SubmitObservation) -> DeliveryState:
    """Map observations to the four-way :class:`DeliveryState` (D7).

    Order matters (spec D7):
      1. A failure demonstrably BEFORE dispatch -> ``nothing-sent`` (retryable).
      2. A matching user node / server acceptance / resolved conversation id
         tied to the submit -> ``delivered`` (read only, never resend).
      3. Deadline exhausted with no confirming signal -> ``timeout``.
      4. Submit attempted, composer cleared, but nothing correlated -> the
         genuinely undetermined ``ack-unknown`` (reconcile, never resend).
      5. Enter never dispatched and no pre-validation failure recorded ->
         ``nothing-sent`` (the prompt never left the composer).
    """
    if obs.pre_validation_failed or not obs.enter_dispatched:
        # Nothing left the composer: safe to correct and retry.
        return DeliveryState.NOTHING_SENT

    delivered = obs.new_user_turn or obs.send_response_seen or bool(obs.conversation_id)
    if delivered:
        return DeliveryState.DELIVERED

    if obs.deadline_exhausted:
        # The window closed carrying the last (unconfirmed) state.
        return DeliveryState.TIMEOUT

    # Submit was attempted; the composer emptied but no correlated evidence
    # arrived. Genuinely unknown — reconcile the SAME conversation, never resend.
    return DeliveryState.ACK_UNKNOWN
